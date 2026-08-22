import os
import shutil
from pathlib import Path
from django.test import TestCase, Client
from .models import EDI835File
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

        # 3. output/ folder has converted .mir file using uploaded base name
        out_mir = self.dirs["output"] / "TEST_RUN_FILE.mir"
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
        output_file = self.dirs["output"] / res["combined_filename"]
        self.assertTrue(os.path.exists(output_file))

        # Check DB record
        db_rec = res["db_record"]
        self.assertEqual(db_rec.status, "ARCHIVED")
        self.assertIn("file_a.835", db_rec.original_filename)
        self.assertIn("file_b.835", db_rec.original_filename)

