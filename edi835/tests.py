import os
import shutil
import tempfile
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from django.test import TestCase, Client, override_settings
from unittest.mock import patch
from accounts.models import Client as AccountClient, User
from .models import EDI835File, MIRFile, MIRServiceLine, RECONFile, ReconciliationReviewAction
from .mir_persistence import store_mir_file
from .services import (
    get_edi835_storage_dirs,
    normalize_mir_generation_result,
    process_edi835_file_content,
    unique_mir_filename,
)
from .file_types import has_valid_file_extension

SAMPLE_835_VALID = "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *260813*1200*U*00501*000000001*0*P*:~GS*HP*SENDER*RECEIVER*20260813*1200*1*X*005010X221A1~ST*835*0001~BPR*I*150.00*C*CHK************20260813~TRN*1*123456789*1999999999~N1*PR*PAYER NAME~N1*PE*PROVIDER NAME*XX*1234567890~LX*1~CLP*CLM_PAYP_20260807*1*200.00*150.00*50.00*MC*REF12345~NM1*QC*1*SMITH*JOHN*M~NM1*IL*1*SMITH*JOHN****MI*SUB123456~REF*1L*GRP999~DTM*036*19850101~DTM*050*20260801~SVC*HC:99213*200.00*150.00**1~DTM*472*20260805~CAS*CO*45*50.00~SE*16*0001~GE*1*1~IEA*1*000000001~"

SAMPLE_835_INVALID = "INVALID_CONTENT_NO_CLP_HEADER"


class MIRGenerationResultContractTestCase(TestCase):
    def test_plain_text_result_is_not_unpacked_character_by_character(self):
        from admin_panel.mir_mapper_logic.models import Claim, ServiceLine

        claims = [Claim(claim_number="CLAIM-1", services=[ServiceLine(), ServiceLine()])]
        text = "A" * 8238

        mir_text, summary = normalize_mir_generation_result(text, claims)

        self.assertEqual(mir_text, text)
        self.assertEqual(summary["claims"], 1)
        self.assertEqual(summary["services"], 2)
        self.assertEqual(summary["mir_records"], 1)

    def test_dictionary_result_uses_generator_counts(self):
        mir_text, summary = normalize_mir_generation_result(
            {"text": "ROW\n", "claims_count": 3, "services_count": 4, "records_count": 1},
            [],
        )

        self.assertEqual(mir_text, "ROW\n")
        self.assertEqual(summary, {"claims": 3, "services": 4, "mir_records": 1})


class EDI835PipelineLifecycleTestCase(TestCase):

    def setUp(self):
        self.client = Client()
        self.dirs = get_edi835_storage_dirs()

    def tearDown(self):
        # Clean up test files from storage directories
        for key, folder in self.dirs.items():
            if key != "base" and os.path.exists(folder):
                for f in os.listdir(folder):
                    file_path = os.path.join(folder, f)
                    if os.path.isfile(file_path):
                        os.remove(file_path)

    def test_successful_lifecycle_archive_only_x12(self):
        original_name = "TEST_RUN_FILE.x12"
        res = process_edi835_file_content(SAMPLE_835_VALID, original_filename=original_name)

        self.assertTrue(res["success"])
        db_rec = res["db_record"]
        stored_name = db_rec.stored_filename

        # Check DB tracking properties
        self.assertEqual(db_rec.status, "ARCHIVED")
        self.assertEqual(db_rec.original_filename, original_name)
        self.assertTrue(db_rec.stored_filename.endswith(original_name))

        # Verify folder states after successful completion:
        # 1. input/ folder is empty
        input_file = self.dirs["input"] / stored_name
        self.assertFalse(os.path.exists(input_file))

        # 2. processing/ folder is empty
        proc_file = self.dirs["processing"] / stored_name
        self.assertFalse(os.path.exists(proc_file))

        # 3. output/ folder has the namespaced MIR file recorded by the pipeline
        out_mir = self.dirs["output"] / Path(db_rec.output_path).name
        self.assertTrue(os.path.exists(out_mir))

        # 4. archive/ folder contains ONLY the x12/835 file (no .mir file in archive/)
        arch_835 = self.dirs["archive"] / stored_name
        arch_mir = self.dirs["archive"] / "TEST_RUN_FILE.mir"
        self.assertTrue(os.path.exists(arch_835))
        self.assertFalse(os.path.exists(arch_mir))

    def test_file_extension_policy_is_case_insensitive(self):
        self.assertTrue(has_valid_file_extension('CLAIM.835', '835'))
        self.assertTrue(has_valid_file_extension('REFERENCE.X12', '837'))
        self.assertTrue(has_valid_file_extension('RECON.P7A', 'RECON'))
        self.assertFalse(has_valid_file_extension('claim.pdf', '835'))

    def test_every_configured_835_and_recon_extension_is_accepted(self):
        from .file_types import allowed_extensions

        for kind in ('835', 'RECON'):
            for extension in allowed_extensions(kind):
                with self.subTest(kind=kind, extension=extension):
                    self.assertTrue(
                        has_valid_file_extension(f'upload{extension}', kind)
                    )
                    self.assertTrue(
                        has_valid_file_extension(f'upload{extension.upper()}', kind)
                    )

    def test_835_and_recon_extension_policies_reject_unrelated_files(self):
        for kind in ('835', 'RECON'):
            for filename in ('upload.pdf', 'upload.exe', 'upload.zip', 'upload'):
                with self.subTest(kind=kind, filename=filename):
                    self.assertFalse(has_valid_file_extension(filename, kind))

    def test_simultaneous_jobs_receive_distinct_mir_names(self):
        first = unique_mir_filename('MIROUT.MIR', '11111111-1111-1111-1111-111111111111')
        second = unique_mir_filename('MIROUT.MIR', '22222222-2222-2222-2222-222222222222')
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith('.MIR'))

    def test_error_lifecycle(self):
        original_name = "BAD_FILE_123.x12"
        res = process_edi835_file_content(SAMPLE_835_INVALID, original_filename=original_name)

        self.assertFalse(res["success"])
        db_rec = res["db_record"]
        stored_name = db_rec.stored_filename

        # Check DB status is ERROR
        self.assertEqual(db_rec.status, "ERROR")

        # Verify folder states after error:
        # 1. input/ and processing/ are empty
        self.assertFalse(os.path.exists(self.dirs["input"] / stored_name))
        self.assertFalse(os.path.exists(self.dirs["processing"] / stored_name))

        # 2. error/ folder contains the failed file with original filename
        err_file = self.dirs["error"] / stored_name
        self.assertTrue(os.path.exists(err_file))

    @patch("edi835.services.parse_835_to_mir", side_effect=RuntimeError("stop after date capture"))
    @patch("edi835.services.EDI835Validator")
    def test_regeneration_reuses_original_process_date(self, validator, parse_835_to_mir):
        validator.return_value.validate.return_value = {"valid": True}
        started_at = datetime(2026, 8, 31, 16, 30, tzinfo=dt_timezone.utc)
        record = EDI835File.objects.create(
            original_filename="repeat.835",
            stored_filename="repeat.835",
            processing_started_at=started_at,
        )

        process_edi835_file_content(
            SAMPLE_835_VALID, original_filename="repeat.835", file_id=record.id
        )

        self.assertEqual(
            parse_835_to_mir.call_args.kwargs["process_date"],
            started_at.astimezone().date(),
        )
        record.refresh_from_db()
        self.assertEqual(record.processing_started_at, started_at)

    def test_multiple_files_single_mir(self):
        from .services import process_multiple_edi835_files
        files_list = [
            {"filename": "file_a.835", "content": SAMPLE_835_VALID},
            {"filename": "file_b.835", "content": SAMPLE_835_VALID.replace("CLM_PAYP_20260807", "CLM_PAYP_BATCH_2")},
        ]
        res = process_multiple_edi835_files(files_list)
        self.assertTrue(res["success"])
        self.assertEqual(res["files_count"], 2)
        self.assertEqual(res["claims_count"], 2)
        
        # Verify single MIR file was created in output directory
        output_file = self.dirs["output"] / res["stored_filename"]
        self.assertTrue(os.path.exists(output_file))

        # Check DB record
        db_rec = res["db_record"]
        self.assertEqual(db_rec.status, "ARCHIVED")
        self.assertEqual(db_rec.ingestion_source, "SFTP")
        self.assertIn("file_a.835", db_rec.original_filename)
        self.assertIn("file_b.835", db_rec.original_filename)

    @patch("admin_panel.mir_mapper_logic.edi835_parser.parse_835")
    def test_sftp_batch_validation_failure_never_reaches_parser(self, parse_835):
        from .services import process_multiple_edi835_files

        result = process_multiple_edi835_files([
            {"filename": "invalid.835", "content": SAMPLE_835_INVALID},
        ], ingestion_source="SFTP")

        self.assertFalse(result["success"])
        self.assertIn("validation failed", result["error"].lower())
        parse_835.assert_not_called()
        record = result["db_record"]
        self.assertEqual(record.status, "ERROR")
        self.assertEqual(record.ingestion_source, "SFTP")


class EDI837ParserTestCase(TestCase):
    def test_professional_sv1_service(self):
        from .recon_service import parse_837_rows

        rows = parse_837_rows("CLM*P-1*125.50~~~SV1*HC:99213*125.50*UN*2~")

        service = rows[0]["services"][0]
        self.assertEqual(service["service_type"], "SV1")
        self.assertEqual(service["procedure_code"], "99213")
        self.assertEqual(service["charge_amount"], Decimal("125.50"))
        self.assertEqual(service["units"], Decimal("2"))

    def test_institutional_sv2_service(self):
        from .recon_service import parse_837_rows

        rows = parse_837_rows("CLM*I-1*450.00~~~SV2*0450*HC:99284*450.00*UN*1~")

        service = rows[0]["services"][0]
        self.assertEqual(service["service_type"], "SV2")
        self.assertEqual(service["revenue_code"], "0450")
        self.assertEqual(service["procedure_code"], "99284")
        self.assertEqual(service["charge_amount"], Decimal("450.00"))
        self.assertEqual(service["units"], Decimal("1"))

    def test_dental_sv3_service(self):
        from .recon_service import parse_837_rows

        rows = parse_837_rows("CLM*D-1*210.00~~~SV3*AD:D0120*210.00***1*3~")

        service = rows[0]["services"][0]
        self.assertEqual(service["service_type"], "SV3")
        self.assertEqual(service["procedure_code"], "D0120")
        self.assertEqual(service["charge_amount"], Decimal("210.00"))
        self.assertEqual(service["units"], Decimal("3"))

    def test_claim_without_supported_service_segment_is_rejected(self):
        from .recon_service import parse_837_rows

        with self.assertRaisesRegex(ValueError, "no supported SV1, SV2, or SV3"):
            parse_837_rows("CLM*EMPTY-1*999.00~")


class MIRPersistenceTestCase(TestCase):
    def test_result_amount_uses_position_429_for_every_service(self):
        from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
        from admin_panel.mir_mapper_logic.models import Claim, ServiceLine

        paid_values = [Decimal("124.89"), Decimal("19.61"), Decimal("19.61")] + [Decimal("0.00")] * 9
        mir_text, _ = generate_mir_text([Claim(
            claim_number="AMOUNT-POSITION-01",
            status="1",
            group_number="TESTGRP",
            services=[ServiceLine(charge=value, paid=value) for value in paid_values],
        )])
        line = mir_text.splitlines()[0]
        self.assertEqual(int(line[332:334]), 12)
        expected_raw = ["0000012489+", "0000001961+", "0000001961+"] + ["0000000000+"] * 9
        for number, expected in enumerate(expected_raw, start=1):
            start = 429 - 1 + ((number - 1) * 303)
            self.assertEqual(line[start:start + 11], expected)

        source = EDI835File.objects.create(original_filename="amount.835", stored_filename="amount.835")
        mir_file = store_mir_file(source_835=source, mir_filename="amount.MIR", mir_text=mir_text)
        stored_total = sum(
            mir_file.claims.get().service_lines.values_list("paid_amount", flat=True),
            Decimal("0"),
        )
        self.assertEqual(stored_total, Decimal("164.11"))

    def test_complete_23_character_reconciliation_id_is_persisted(self):
        from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
        from admin_panel.mir_mapper_logic.models import Claim, ServiceLine

        mir_text, _ = generate_mir_text([Claim(
            claim_number="86520261762674200", claim_reference="QZL067",
            status="1", group_number="TESTGRP",
            services=[ServiceLine(charge=Decimal("75.00"), paid=Decimal("60.00"))],
        )])
        source = EDI835File.objects.create(original_filename="id.835", stored_filename="id.835")
        mir_file = store_mir_file(source_835=source, mir_filename="id.MIR", mir_text=mir_text)
        claim = mir_file.claims.get()
        self.assertEqual(claim.claim_control_number, "86520261762674200QZL067")
        self.assertEqual(claim.service_lines.get().charge_amount, Decimal("75.00"))
        self.assertEqual(claim.service_lines.get().paid_amount, Decimal("60.00"))
    def test_reference_mir_fixed_width_recon_uses_exact_positions(self):
        from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
        from admin_panel.mir_mapper_logic.models import Claim, ServiceLine
        from .recon_service import _money_decimal, parse_recon_rows

        mir_text, _ = generate_mir_text([Claim(
            claim_number="37620261920034300", claim_reference="QXW615",
            status="1", group_number="TESTGRP",
            services=[
                ServiceLine(charge=Decimal("50.00"), paid=Decimal("40.00")),
                ServiceLine(charge=Decimal("25.00"), paid=Decimal("20.00")),
            ],
        )])
        rows = parse_recon_rows(mir_text)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["data"]["claim_control_number"] == "37620261920034300QXW615" for row in rows))
        self.assertEqual([row["data"]["paid_amount"] for row in rows], ["0000004000+", "0000002000+"])
        self.assertEqual([row["data"]["allowed_amount"] for row in rows], ["0000005000+", "0000002500+"])
        self.assertEqual(
            sum((_money_decimal(row["data"]["paid_amount"]) for row in rows), Decimal("0")),
            Decimal("60.00"),
        )

    def test_mir_persistence_carries_allowed_amount_and_mp003_inputs(self):
        from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
        from admin_panel.mir_mapper_logic.models import Adjustment, Claim, ServiceLine

        service = ServiceLine(
            charge=Decimal("100.00"),
            paid=Decimal("70.00"),
            adjustments=[
                Adjustment(group="CO", reason="45", amount=Decimal("20.00")),
                Adjustment(group="PR", reason="1", amount=Decimal("10.00")),
            ],
        )
        mir_text, summary = generate_mir_text([
            Claim(claim_number="ALLOWED-1", status="1", group_number="TESTGRP", services=[service])
        ])
        self.assertEqual(summary["findings"], [])
        source = EDI835File.objects.create(
            original_filename="allowed.835", stored_filename="allowed.835"
        )
        stored = store_mir_file(source_835=source, mir_filename="allowed.MIR", mir_text=mir_text)
        line = stored.claims.get().service_lines.get()

        self.assertEqual(line.allowed_amount, Decimal("80.00"))
        self.assertEqual(line.allowed_amount, line.paid_amount + line.patient_liability)

    def test_claim_over_50_services_is_stored_as_continuation_chunks(self):
        from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
        from admin_panel.mir_mapper_logic.models import Claim, ServiceLine

        services = [
            ServiceLine(charge=Decimal("10.00"), paid=Decimal("8.00"))
            for _ in range(51)
        ]
        mir_text, summary = generate_mir_text([
            Claim(
                claim_number="CONTINUATION00001",
                status="1",
                group_number="TESTGRP",
                services=services,
            )
        ])
        source = EDI835File.objects.create(
            original_filename="split.835",
            stored_filename="split.835",
        )

        mir_file = store_mir_file(
            source_835=source,
            mir_filename="split.MIR",
            mir_text=mir_text,
        )
        claim = mir_file.claims.get()

        self.assertEqual(summary["mir_records"], 2)
        self.assertEqual(mir_file.file_content, mir_text)
        self.assertEqual(mir_file.physical_row_count, 2)
        self.assertEqual(claim.chunk_count, 2)
        self.assertEqual(claim.service_count, 51)
        self.assertEqual(
            list(claim.chunks.values_list("services_in_chunk", flat=True)),
            [50, 1],
        )
        self.assertEqual(
            list(claim.chunks.values_list("service_start_number", "service_end_number")),
            [(1, 50), (51, 51)],
        )
        self.assertEqual(
            MIRServiceLine.objects.filter(mir_claim=claim).count(),
            51,
        )


class DurableBatchJobTestCase(TestCase):
    def test_job_status_is_durable_and_interrupted_jobs_fail_safely(self):
        from .batch_jobs import active_job_for, read_job, recover_interrupted_jobs, write_job

        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            job = {
                "id": "11111111-1111-1111-1111-111111111111",
                "owner_user_id": "7",
                "client_id": "client-1",
                "scope_key": "client-1",
                "state": "RUNNING",
                "started_at": "2026-08-27T00:00:00+00:00",
            }
            write_job(job)
            self.assertEqual(read_job(job["id"])["state"], "RUNNING")
            self.assertEqual(active_job_for("client-1")["id"], job["id"])
            self.assertEqual(recover_interrupted_jobs(), 1)
            recovered = read_job(job["id"])
            self.assertEqual(recovered["state"], "FAILED")
            self.assertIn("safe retry", recovered["result"]["error"])


@override_settings(RECON_PROCESS_SYNCHRONOUS=True, SECURE_SSL_REDIRECT=False)
class RECONResultAPITestCase(TestCase):
    def setUp(self):
        self.tenant = AccountClient.objects.create(
            name="Test Health Plan",
            client_code="TESTHP",
            email="plan@example.com",
        )
        self.other_tenant = AccountClient.objects.create(
            name="Other Health Plan",
            client_code="OTHER",
            email="other@example.com",
        )
        self.user = User.objects.create_user(
            email="result@example.com",
            name="Result User",
            mobile="1111111111",
            password="test-password",
            client=self.tenant,
        )
        self.client.force_login(self.user)

    def test_dashboard_comparison_counts_include_aged_missing_recon_claims(self):
        from datetime import timedelta
        from django.utils import timezone
        from .recon_views import _comparison_counts

        old_date = (timezone.now() - timedelta(days=9)).isoformat()
        rows = [
            {"mir_claim_id": 1, "recon_filename": "a.p7a", "amount_to_pay": "10", "recon_paid_amount": "10"},
            {"mir_claim_id": 2, "recon_filename": "a.p7a", "amount_to_pay": "20", "recon_paid_amount": "10"},
            {"mir_claim_id": 3, "recon_filename": "a.p7a", "amount_to_pay": "10", "recon_paid_amount": "20"},
            {"mir_claim_id": 4, "recon_filename": "", "amount_to_pay": "10", "recon_paid_amount": "0", "mir_date": old_date},
        ]

        self.assertEqual(_comparison_counts(rows), {
            "MIR_EQ_RECON": 1, "MIR_GT_RECON": 1, "MIR_LT_RECON": 1,
            "NOT_IN_RECON": 1, "AGED_NOT_IN_RECON": 1,
        })

    def test_cash_summary_uses_real_claim_amounts(self):
        from .recon_views import _cash_summary

        rows = [
            {"amount_to_pay": "100.00", "recon_paid_amount": "100.00"},
            {"amount_to_pay": "50.00", "recon_paid_amount": "70.00"},
            {"amount_to_pay": "80.00", "recon_paid_amount": "40.00"},
            {"amount_to_pay": "30.00", "recon_paid_amount": "0.00"},
            {"amount_to_pay": "0.00", "recon_paid_amount": "10.00"},
        ]

        self.assertEqual(_cash_summary(rows), {
            "total_amount_in_mir": "260.00",
            "total_amount_in_recon": "220.00",
            "overpaid": "30.00",
            "underpaid": "70.00",
        })

    def test_reconciliation_review_action_is_persisted_for_client_claim(self):
        response = self.client.post(
            "/edi835/api/reconciliation/actions/",
            data='{"claim_id":"CLAIM-REVIEW-1","action_status":"IN_PROCESS"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        action = ReconciliationReviewAction.objects.get(
            scope_key=str(self.tenant.id), claim_control_number="CLAIM-REVIEW-1"
        )
        self.assertEqual(action.client, self.tenant)
        self.assertEqual(action.action_status, "IN_PROCESS")
        self.assertEqual(action.updated_by, self.user)

    def test_excel_export_handles_three_filtered_rows_without_unpacking_them(self):
        from io import BytesIO
        from unittest.mock import patch

        rows = [{"claim_id": f"CLAIM-{index}"} for index in range(1, 4)]
        workbook = BytesIO(b"xlsx")
        with (
            patch("edi835.recon_views.reconciliation_rows", return_value=rows),
            patch("edi835.recon_views.build_reconciliation_workbook", return_value=workbook) as build,
        ):
            response = self.client.get("/edi835/api/reconciliation/export/?status=PARTIALLY_PAID")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"xlsx")
        self.assertEqual(build.call_args.kwargs["rows"], rows)
        self.assertEqual(build.call_args.kwargs["total"], 3)

    def test_file_dashboard_is_scoped_to_the_opened_database_record(self):
        source = EDI835File.objects.create(
            client=self.tenant, original_filename="cycle.835", stored_filename="cycle.835",
        )
        MIRFile.objects.create(
            source_835=source, client=self.tenant, mir_filename="cycle.MIR",
            file_content="MIR", file_hash="d" * 64, file_size=3,
        )
        RECONFile.objects.create(
            client=self.tenant, original_filename="cycle.csv", stored_filename="cycle.csv",
            file_content="RECON", file_hash="e" * 64, file_size=5, status="PROCESSED",
        )
        rows = [{
            "claim_id": "CLAIM1", "amount_to_pay": "100.00", "recon_paid_amount": "118.00",
            "recon_fees": {"MIR904": "5.00", "MIR905": "10.00", "MPL920": "3.00"},
            "difference_amount": "0.00", "status": "CLEAR", "match_step": "MPL920",
            "affected_by_interim_policy": True, "policy_flags": [],
        }]

        with patch("edi835.recon_views.reconciliation_rows", return_value=rows) as reconcile:
            response = self.client.get(f"/edi835/api/reconciliation/files/{source.id}/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["source"]["filename"], "cycle.835")
        self.assertEqual(payload["cash"]["unexplained"], "0.00")
        self.assertEqual(payload["cash"]["overpaid"], "0")
        self.assertEqual(payload["cash"]["underpaid"], "0")
        self.assertEqual(payload["tallies"]["matched_with_caveat"], 1)
        self.assertEqual(payload["records"][0]["mpl920"], "3.00")
        self.assertEqual(reconcile.call_args.kwargs["mir_file_id"], source.mir_file.id)
        self.assertFalse(reconcile.call_args.kwargs["include_match_history"])

    def test_upload_process_and_tenant_scoped_listing(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        content = (
            "Claim ID,Member ID,Line Number,Procedure Code,Charge Amount,Paid Amount\n"
            "CLAIM-100,MEMBER-1,1,99213,100.00,80.00\n"
            "CLAIM-100,MEMBER-1,2,99214,50.00,40.00\n"
            "CLAIM-200,MEMBER-2,1,99215,75.00,60.00\n"
        )
        response = self.client.post(
            "/edi835/api/recon/upload/",
            {"recon_file": SimpleUploadedFile("recon.csv", content.encode("utf-8"), content_type="text/csv")},
        )
        self.assertEqual(response.status_code, 201)
        file_id = response.json()["file"]["id"]

        response = self.client.post(f"/edi835/api/recon/files/{file_id}/process/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["file"]["claim_count"], 2)
        self.assertEqual(response.json()["file"]["service_count"], 3)
        recon = RECONFile.objects.get(id=file_id)
        self.assertEqual(recon.claims.count(), 2)
        self.assertEqual(recon.service_lines.count(), 3)
        self.assertEqual(str(recon.total_charge_amount), "225.00")
        self.assertEqual(str(recon.total_paid_amount), "180.00")

        RECONFile.objects.create(
            client=self.other_tenant,
            original_filename="hidden.csv",
            stored_filename="hidden.csv",
            file_content="hidden",
            file_hash="f" * 64,
            file_size=6,
        )
        response = self.client.get("/edi835/api/recon/files/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["files"]], [file_id])

        download = self.client.get(f"/edi835/api/recon/files/{file_id}/download/")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content.decode("utf-8"), content)

        self.assertIn('filename="recon.csv"', download["Content-Disposition"])

        hidden = RECONFile.objects.get(client=self.other_tenant)
        self.assertEqual(
            self.client.get(f"/edi835/api/recon/files/{hidden.id}/download/").status_code,
            404,
        )

        results = self.client.get("/edi835/api/reconciliation/")
        self.assertEqual(results.status_code, 200)
        self.assertEqual(results.json()["total_claims"], 2)
        recon_only = {row["claim_id"]: row for row in results.json()["claims"]}
        self.assertEqual(recon_only["CLAIM100"]["status"], "NOT_IN_MIR")
        self.assertIsNone(recon_only["CLAIM100"]["mir_claim_id"])
        self.assertEqual(Decimal(recon_only["CLAIM100"]["recon_paid_amount"]), Decimal("120.00"))
        self.assertEqual(Decimal(recon_only["CLAIM100"]["difference_amount"]), Decimal("120.00"))
        self.assertEqual(recon_only["CLAIM100"]["recon_matches"][0]["filename"], "recon.csv")

        status_filter = self.client.get("/edi835/api/reconciliation/?status=NOT_IN_MIR")
        self.assertEqual(status_filter.status_code, 200)
        self.assertEqual(status_filter.json()["total_claims"], 2)
        self.assertTrue(all(row["status"] == "NOT_IN_MIR" for row in status_filter.json()["claims"]))

        comma_search = self.client.get(
            "/edi835/api/reconciliation/?search=CLAIM-100%2C%20CLAIM-200"
        )
        self.assertEqual(comma_search.status_code, 200)
        self.assertEqual(comma_search.json()["total_claims"], 2)
        self.assertEqual(
            {row["claim_id"] for row in comma_search.json()["claims"]},
            {"CLAIM100", "CLAIM200"},
        )

        export = self.client.get(
            "/edi835/api/reconciliation/export/?search=CLAIM-100&status=NOT_IN_MIR"
        )
        self.assertEqual(export.status_code, 200)
        self.assertEqual(
            export["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        from io import BytesIO
        from openpyxl import load_workbook
        workbook = load_workbook(BytesIO(export.content), data_only=False)
        sheet = workbook["Reconciliation Results"]
        self.assertEqual(sheet["A1"].value, "MIR / RECON Reconciliation Results")
        self.assertEqual(sheet._images, [])
        self.assertEqual(sheet["B5"].value, 1)
        self.assertEqual(sheet["A11"].value, "CLAIM100")
        self.assertEqual(sheet["H11"].value, "-")
        self.assertEqual(sheet["I11"].value, "Not In Mir")

    def test_invalid_recon_row_is_held_without_inventing_financial_data(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        content = (
            "Claim ID,Member ID,Paid Amount,MIR904,MIR905,MPL920\n"
            "CLAIM-VALID,MEMBER-1,98.00,5.00,10.00,3.00\n"
            ",MEMBER-2,999.00,0.00,0.00,0.00\n"
        )
        upload = self.client.post("/edi835/api/recon/upload/", {
            "recon_file": SimpleUploadedFile("partial.csv", content.encode("utf-8"))
        }).json()

        response = self.client.post(f"/edi835/api/recon/files/{upload['file']['id']}/process/")

        self.assertEqual(response.status_code, 200)
        recon = RECONFile.objects.get(id=upload["file"]["id"])
        self.assertEqual(recon.status, "PARTIAL")
        self.assertEqual(recon.claim_count, 1)
        self.assertEqual(recon.held_record_count, 1)
        claim = recon.claims.get()
        self.assertEqual(claim.claim_control_number, "CLAIM-VALID")
        self.assertEqual(claim.mir904_bluecard_fee, Decimal("5.00"))
        self.assertEqual(claim.mir905_aea, Decimal("10.00"))
        self.assertEqual(claim.mpl920_pca_fee, Decimal("3.00"))
        self.assertFalse(recon.claims.filter(claim_control_number__startswith="ROW-").exists())
        self.assertEqual(recon.processing_errors.get().error_code, "MISSING_CLAIM_IDENTIFIER")
        detail = self.client.get(f"/edi835/api/recon/files/{recon.id}/")
        self.assertEqual(detail.status_code, 200)
        held = detail.json()["errors"][0]
        self.assertEqual(held["claim_control_number"], "")
        self.assertEqual(held["error_code"], "MISSING_CLAIM_IDENTIFIER")
        self.assertIn("MEMBER-2", held["raw_record"])

    def test_unrecognized_fixed_width_row_does_not_guess_last_money_token(self):
        from .recon_service import parse_recon_rows

        rows, findings = parse_recon_rows(
            "CLAIM-FAKE arbitrary values 10.00 999.99", include_findings=True
        )

        self.assertEqual(rows, [])
        self.assertEqual(findings[0]["error_code"], "UNRECOGNIZED_RECON_LAYOUT")
        self.assertNotIn("paid_amount", findings[0])

    def test_legacy_p7a_row_uses_full_known_claim_key_and_last_amount(self):
        from .recon_service import parse_recon_rows

        claim_id = "12345678901234567ABC123"
        rows, findings = parse_recon_rows(
            f"P7A REPORT {claim_id} CHARGE 125.00 PAID 98.25",
            known_claim_ids=[claim_id],
            include_findings=True,
        )

        self.assertEqual(findings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["data"]["claim_control_number"], claim_id)
        self.assertEqual(rows[0]["data"]["paid_amount"], "98.25")
        self.assertEqual(rows[0]["data"]["fixed_width_legacy_p7a"], "1")

    def test_legacy_fixed_width_row_without_full_claim_key_is_held(self):
        from .recon_service import parse_recon_rows

        rows, findings = parse_recon_rows(
            "P7A REPORT CLAIM-123 CHARGE 125.00 PAID 98.25",
            known_claim_ids=["12345678901234567ABC123"],
            include_findings=True,
        )

        self.assertEqual(rows, [])
        self.assertEqual(findings[0]["error_code"], "UNRECOGNIZED_RECON_LAYOUT")

    def test_dos_eof_marker_is_not_parsed_as_a_recon_record(self):
        from .recon_service import parse_recon_rows

        rows, findings = parse_recon_rows(
            "Claim ID,Paid Amount\r\nCLAIM-1,10.00\r\n\x1a\r\n",
            include_findings=True,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["data"]["claim_control_number"], "CLAIM-1")
        self.assertEqual(findings, [])

    @override_settings(
        MPL_RECON_MIR907_SOURCE="computed",
        MPL_RECON_MIR908_SOURCE="computed",
        MPL_RECON_INCLUDE_MPL920=True,
        MPL_RECON_WATERFALL_STEPS=("MIR901", "MIR907", "MIR908", "MPL920"),
    )
    def test_fee_waterfall_records_the_step_that_clears_the_claim(self):
        from .reconciliation_service import reconciliation_waterfall

        match = reconciliation_waterfall(
            Decimal("100.00"), Decimal("118.00"), True,
            {"mir904": Decimal("5.00"), "mir905": Decimal("10.00"),
             "mpl920": Decimal("3.00")},
        )

        self.assertEqual(match["status"], "CLEAR")
        self.assertEqual(match["match_step"], "MPL920")
        self.assertEqual(match["candidates"], {
            "MIR901": "100.00", "MIR907": "105.00",
            "MIR908": "115.00", "MPL920": "118.00",
        })
        self.assertTrue(match["affected_by_interim_policy"])

    def test_binary_recon_is_rejected_before_database_write(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(
            "/edi835/api/recon/upload/",
            {"recon_file": SimpleUploadedFile("binary.p7a", b"valid-prefix\x00binary-data")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("contains binary data", response.json()["error"])
        self.assertFalse(RECONFile.objects.filter(original_filename="binary.p7a").exists())

    def test_reconciliation_aggregates_mir_chunks_and_uses_latest_recon(self):
        from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
        from admin_panel.mir_mapper_logic.models import Claim, ServiceLine
        from django.core.files.uploadedfile import SimpleUploadedFile

        services = [ServiceLine(charge=Decimal("10.00"), paid=Decimal("8.00")) for _ in range(75)]
        mir_text, _ = generate_mir_text([Claim(
            claim_number="CLAIM-75", status="1", subscriber_id="MEMBER-75",
            patient_first_name="Jane", patient_last_name="Doe",
            group_number="TESTGRP", services=services,
        )])
        source = EDI835File.objects.create(
            client=self.tenant, original_filename="source.835", stored_filename="source.835",
        )
        store_mir_file(source_835=source, mir_filename="source.MIR", mir_text=mir_text)
        content = (
            "Claim ID,Member ID,Line Number,Charge Amount,Paid Amount\n"
            "claim_75,MEMBER-75,1,750.00,600.00\n"
        )
        uploaded = self.client.post("/edi835/api/recon/upload/", {
            "recon_file": SimpleUploadedFile("latest.csv", content.encode(), content_type="text/csv")
        }).json()
        self.client.post(f"/edi835/api/recon/files/{uploaded['file']['id']}/process/")

        response = self.client.get("/edi835/api/reconciliation/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_claims"], 1)
        self.assertEqual(response.json()["page"], 1)
        self.assertEqual(response.json()["total_pages"], 1)
        row = response.json()["claims"][0]
        self.assertEqual(row["claim_id"], "CLAIM75")
        self.assertEqual(row["mir_service_count"], 75)
        self.assertEqual(Decimal(row["mir_charge_amount"]), Decimal("750.00"))
        self.assertEqual(Decimal(row["mir_allowed_amount"]), Decimal("750.00"))
        self.assertEqual(Decimal(row["amount_to_pay"]), Decimal("600.00"))
        self.assertEqual(Decimal(row["mir_patient_liability"]), Decimal("150.00"))
        self.assertTrue(row["mp003_cross_foot_valid"])
        self.assertEqual(Decimal(row["recon_paid_amount"]), Decimal("600.00"))
        self.assertEqual(row["recon_filename"], "latest.csv")
        self.assertEqual(len(row["recon_matches"]), 1)
        self.assertEqual(row["recon_matches"][0]["filename"], "latest.csv")
        self.assertEqual(Decimal(row["recon_matches"][0]["paid_amount"]), Decimal("600.00"))
        self.assertEqual(row["status"], "CLEAR")

        search_response = self.client.get("/edi835/api/reconciliation/?search=MEMBER-75")
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(search_response.json()["total_claims"], 1)
        self.assertEqual(search_response.json()["claims"][0]["claim_id"], "CLAIM75")

        recon_search = self.client.get("/edi835/api/reconciliation/?search=latest.csv")
        self.assertEqual(recon_search.status_code, 200)
        self.assertEqual(recon_search.json()["total_claims"], 1)
        self.assertEqual(recon_search.json()["claims"][0]["recon_filename"], "latest.csv")

        status_sort = self.client.get(
            "/edi835/api/reconciliation/?sort_by=status&sort_direction=desc&page_size=25"
        )
        self.assertEqual(status_sort.status_code, 200)
        self.assertEqual(status_sort.json()["claims"][0]["status"], "CLEAR")

        invalid_sort = self.client.get("/edi835/api/reconciliation/?sort_by=unknown")
        self.assertEqual(invalid_sort.status_code, 400)

    def test_reconciliation_statuses_not_in_recon_and_signature_mismatch(self):
        from .reconciliation_service import reconciliation_status
        self.assertEqual(reconciliation_status(Decimal("10"), Decimal("0"), False)[0], "NOT_IN_RECON")
        self.assertEqual(reconciliation_status(Decimal("10"), Decimal("-10"), True)[0], "SIGNATURE_MISMATCH")
        self.assertEqual(reconciliation_status(Decimal("10"), Decimal("4"), True)[0], "PARTIALLY_PAID")
        self.assertEqual(reconciliation_status(Decimal("10"), Decimal("12"), True)[0], "OVERPAID")

    def test_admin_global_scope_only_returns_global_results(self):
        admin = User.objects.create_superuser(
            email="admin@example.com", name="Admin", mobile="2222222222", password="test-password"
        )
        self.client.force_login(admin)
        global_recon = RECONFile.objects.create(
            client=None, uploaded_by=admin, original_filename="global.csv",
            stored_filename="GLOBAL_global.csv", file_content="Claim ID,Paid Amount\nGLOBAL-1,10.00",
            file_hash="a" * 64, file_size=40, status="PROCESSED",
        )
        RECONFile.objects.create(
            client=self.tenant, original_filename="client.csv", stored_filename="client.csv",
            file_content="client", file_hash="b" * 64, file_size=6, status="PROCESSED",
        )
        response = self.client.get("/edi835/api/recon/files/?scope=global")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["files"]], [str(global_recon.id)])
        self.assertEqual(response.json()["files"][0]["client_name"], "Global System Default")

    def test_super_admin_can_view_selected_client_results_without_temporary_grant(self):
        admin = User.objects.create_superuser(
            email="super-results@example.com",
            name="Super Results",
            mobile="3333333333",
            password="test-password",
        )
        self.client.force_login(admin)

        response = self.client.get(
            f"/edi835/api/reconciliation/?client_id={self.tenant.id}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

    def test_regular_admin_still_needs_temporary_grant_for_client_results(self):
        admin = User.objects.create_user(
            email="regular-results@example.com",
            name="Regular Results",
            mobile="4444444444",
            password="test-password",
            is_staff=True,
        )
        self.client.force_login(admin)

        response = self.client.get(
            f"/edi835/api/reconciliation/?client_id={self.tenant.id}"
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Select a client.")

    def test_mir_claims_are_returned_without_any_recon_file(self):
        from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
        from admin_panel.mir_mapper_logic.models import Claim, ServiceLine

        mir_text, _ = generate_mir_text([
            Claim(
                claim_number="MIR-ONLY-1",
                status="1",
                group_number="TESTGRP",
                services=[ServiceLine(charge=Decimal("25.00"), paid=Decimal("20.00"))],
            )
        ])
        source = EDI835File.objects.create(
            client=self.tenant, original_filename="mir-only.835", stored_filename="mir-only.835",
        )
        store_mir_file(source_835=source, mir_filename="mir-only.MIR", mir_text=mir_text)

        response = self.client.get("/edi835/api/reconciliation/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["claims"]), 1)
        self.assertEqual(response.json()["claims"][0]["claim_id"], "MIRONLY1")
        self.assertEqual(response.json()["claims"][0]["recon_filename"], "")
        self.assertEqual(response.json()["claims"][0]["status"], "NOT_IN_RECON")
