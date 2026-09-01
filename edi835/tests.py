import os
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from django.test import TestCase, Client, override_settings
from accounts.models import Client as AccountClient, User
from .models import EDI835File, MIRServiceLine, RECONFile
from .mir_persistence import store_mir_file
from .services import process_edi835_file_content, get_edi835_storage_dirs, unique_mir_filename
from .file_types import has_valid_file_extension

SAMPLE_835_VALID = "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *260813*1200*U*00501*000000001*0*P*:~GS*HP*SENDER*RECEIVER*20260813*1200*1*X*005010X221A1~ST*835*0001~BPR*I*150.00*C*CHK************20260813~TRN*1*123456789*1999999999~N1*PR*PAYER NAME~N1*PE*PROVIDER NAME*XX*1234567890~LX*1~CLP*CLM_PAYP_20260807*1*200.00*150.00*50.00*MC*REF12345~NM1*QC*1*SMITH*JOHN*M~NM1*IL*1*SMITH*JOHN****MI*SUB123456~REF*1L*GRP999~DTM*036*19850101~DTM*050*20260801~SVC*HC:99213*200.00*150.00**1~DTM*472*20260805~CAS*CO*45*50.00~SE*16*0001~GE*1*1~IEA*1*000000001~"

SAMPLE_835_INVALID = "INVALID_CONTENT_NO_CLP_HEADER"


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
class MIRPersistenceTestCase(TestCase):
    def test_result_amount_uses_position_429_for_every_service(self):
        from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
        from admin_panel.mir_mapper_logic.models import Claim, ServiceLine

        paid_values = [Decimal("124.89"), Decimal("19.61"), Decimal("19.61")] + [Decimal("0.00")] * 9
        mir_text, _ = generate_mir_text([Claim(
            claim_number="AMOUNT-POSITION-01",
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
            services=[
                ServiceLine(charge=Decimal("50.00"), paid=Decimal("40.00")),
                ServiceLine(charge=Decimal("25.00"), paid=Decimal("20.00")),
            ],
        )])
        rows = parse_recon_rows(mir_text)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["data"]["claim_control_number"] == "37620261920034300QXW615" for row in rows))
        self.assertEqual([row["data"]["paid_amount"] for row in rows], ["0000004000+", "0000002000+"])
        self.assertEqual(
            sum((_money_decimal(row["data"]["paid_amount"]) for row in rows), Decimal("0")),
            Decimal("60.00"),
        )

    def test_claim_over_50_services_is_stored_as_continuation_chunks(self):
        from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
        from admin_panel.mir_mapper_logic.models import Claim, ServiceLine

        services = [
            ServiceLine(charge=Decimal("10.00"), paid=Decimal("8.00"))
            for _ in range(51)
        ]
        mir_text, summary = generate_mir_text([
            Claim(claim_number="CONTINUATION00001", services=services)
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


@override_settings(RECON_PROCESS_SYNCHRONOUS=True)
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
            claim_number="CLAIM-75", subscriber_id="MEMBER-75",
            patient_first_name="Jane", patient_last_name="Doe", services=services,
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
        self.assertEqual(Decimal(row["amount_to_pay"]), Decimal("600.00"))
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

    def test_mir_claims_are_returned_without_any_recon_file(self):
        from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
        from admin_panel.mir_mapper_logic.models import Claim, ServiceLine

        mir_text, _ = generate_mir_text([
            Claim(claim_number="MIR-ONLY-1", services=[ServiceLine(charge=Decimal("25.00"), paid=Decimal("20.00"))])
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
