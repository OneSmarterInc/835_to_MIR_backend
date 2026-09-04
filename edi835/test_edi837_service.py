from types import SimpleNamespace

from django.test import SimpleTestCase

from .claim_numbers import split_claim_number
from .edi837_service import export_single_claim, parse_837, split_x12


SAMPLE_837 = (
    "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *260904*1200*^*00501*000000001*0*P*:~"
    "GS*HC*SENDER*RECEIVER*20260904*1200*1*X*005010X222A1~ST*837*0001*005010X222A1~BHT*0019*00*1*20260904*1200*CH~"
    "HL*1**20*1~NM1*85*2*PROVIDER*****XX*1234567890~HL*2*1*22*0~SBR*P*18*******CI~"
    "NM1*IL*1*DOE*JANE****MI*MEMBER1~NM1*PR*2*HIGHMARK*****PI*PAYOR~"
    "CLM*123456789QYN071*13.62***11:B:1*Y*A*Y*Y~REF*9C*EXTERNAL1~HI*ABK:M255~"
    "LX*1~SV1*HC:99213*13.62*UN*1***1~DTP*472*D8*20260901~"
    "CLM*987654321ABC123*20.00***11:B:1*Y*A*Y*Y~REF*9C*EXTERNAL2~"
    "LX*1~SV1*HC:93000*20.00*UN*1***1~SE*20*0001~GE*1*1~IEA*1*000000001~"
)


class EDI837ParsingTests(SimpleTestCase):
    def test_claims_and_service_lines_are_normalized(self):
        parsed = parse_837(SAMPLE_837)
        self.assertEqual(len(parsed["claims"]), 2)
        first = parsed["claims"][0]
        self.assertEqual(first["claim_control_number"], "123456789QYN071")
        self.assertEqual(first["reference_9c"], "EXTERNAL1")
        self.assertEqual(first["member_id"], "MEMBER1")
        self.assertEqual(first["services"][0]["procedure_code"], "99213")

    def test_numeric_claim_is_a_highmark_number(self):
        self.assertEqual(split_claim_number("123456789"), {
            "highmark_claim_number": "123456789", "internal_claim_number": "",
        })

    def test_single_claim_export_has_valid_counts_and_no_sibling_claim(self):
        claim = SimpleNamespace(
            pk=1, claim_control_number="123456789QYN071",
            patient_control_number="123456789QYN071",
            edi_file=SimpleNamespace(file_content=SAMPLE_837),
        )
        output = export_single_claim(claim)
        _, _, _, segments = split_x12(output)
        tags = [segment[0] for segment in segments]
        self.assertEqual(tags.count("CLM"), 1)
        self.assertNotIn("987654321ABC123", output)
        st_index, se_index = tags.index("ST"), tags.index("SE")
        self.assertEqual(int(segments[se_index][1]), se_index - st_index + 1)
