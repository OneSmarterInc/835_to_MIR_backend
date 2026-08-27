import os
import shutil
from decimal import Decimal
from pathlib import Path
from django.test import TestCase, Client
from accounts.models import Client as AccountClient, User
from .models import EDI835File, MIRServiceLine, RECONFile
from .mir_persistence import store_mir_file
from .services import process_edi835_file_content, get_edi835_storage_dirs

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
        self.assertIn("file_a.835", db_rec.original_filename)
        self.assertIn("file_b.835", db_rec.original_filename)
class MIRPersistenceTestCase(TestCase):
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
        mir_file = store_mir_file(source_835=source, mir_filename="source.MIR", mir_text=mir_text)
        mir_file.claims.get().service_lines.update(
            charge_amount=Decimal("10.00"), paid_amount=Decimal("8.00")
        )
        content = (
            "Claim ID,Member ID,Line Number,Charge Amount,Paid Amount\n"
            "CLAIM-75,MEMBER-75,1,750.00,600.00\n"
        )
        uploaded = self.client.post("/edi835/api/recon/upload/", {
            "recon_file": SimpleUploadedFile("latest.csv", content.encode(), content_type="text/csv")
        }).json()
        self.client.post(f"/edi835/api/recon/files/{uploaded['file']['id']}/process/")

        response = self.client.get("/edi835/api/reconciliation/")
        self.assertEqual(response.status_code, 200)
        row = response.json()["claims"][0]
        self.assertEqual(row["claim_id"], "CLAIM-75")
        self.assertEqual(row["mir_service_count"], 75)
        self.assertEqual(Decimal(row["mir_charge_amount"]), Decimal("750.00"))
        self.assertEqual(Decimal(row["amount_to_pay"]), Decimal("600.00"))
        self.assertEqual(Decimal(row["recon_paid_amount"]), Decimal("600.00"))
        self.assertEqual(row["status"], "CLEAR")

    def test_reconciliation_statuses_not_in_recon_and_signature_mismatch(self):
        from .reconciliation_service import reconciliation_status
        self.assertEqual(reconciliation_status(Decimal("10"), Decimal("0"), False)[0], "NOT_IN_RECON")
        self.assertEqual(reconciliation_status(Decimal("10"), Decimal("-10"), True)[0], "SIGNATURE_MISMATCH")
        self.assertEqual(reconciliation_status(Decimal("10"), Decimal("4"), True)[0], "PARTIALLY_PAID")
        self.assertEqual(reconciliation_status(Decimal("10"), Decimal("12"), True)[0], "OVERPAID")
