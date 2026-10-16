from types import SimpleNamespace
from django.test import SimpleTestCase

from .client_claim_slice import claim_slice
from .edi835_claim_service import normalized_835_claims


ISA = "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *260814*1200*^*00501*000000001*0*T*:"
GS = "GS*HP*S*R*20260814*1200*1*X*005010X221A1"


def source(body):
    return f"{ISA}~{GS}~{body}GE*2*1~IEA*1*000000001~"


BODY = ("ST*835*0001~BPR*I*80*C*CHK************20260814~N1*PR*FIRST PAYER~LX*1~"
        "CLP*FIRST*1*100*80*0*12*INT1~NM1*QC*1*FIRST*PATIENT~SVC*HC:111*100*80**1~SE*8*0001~"
        "ST*835*0002~BPR*I*90*C*CHK************20260815~N1*PR*SECOND PAYER~LX*1~"
        "CLP*SECOND*1*120*90*0*12*INT2~NM1*QC*1*SECOND*PATIENT~SVC*HC:222*120*90**1~SE*8*0002~")


class ClientClaimSliceTests(SimpleTestCase):
    def claim(self, content, number="FIRST", sequence=1):
        return SimpleNamespace(edi_file=SimpleNamespace(input_file_content=content),
            highmark_claim_number=number, claim_sequence=sequence, segment_data={}, raw_claim="")

    def assert_envelope(self, result, control):
        self.assertEqual(result["error"], "")
        segments = result["segments"]
        self.assertEqual(sum(s.startswith("CLP*") for s in segments), 1)
        self.assertTrue(segments[0].startswith("ISA*"))
        self.assertEqual(segments[-2:], ["GE*1*1", "IEA*1*000000001"])
        st = next(i for i, s in enumerate(segments) if s.startswith("ST*"))
        se = next(i for i, s in enumerate(segments) if s.startswith("SE*"))
        self.assertEqual(segments[se], f"SE*{se - st + 1}*{control}")
        self.assertTrue(result["content"].endswith("~\n"))

    def test_only_selected_transaction_and_claim_are_returned(self):
        for number, sequence, other in (("FIRST", 1, "SECOND"), ("SECOND", 2, "FIRST")):
            result = claim_slice(self.claim(source(BODY), number, sequence))
            self.assert_envelope(result, f"000{sequence}")
            self.assertNotIn(other, result["content"])
            self.assertIn(number + " PAYER", result["content"])

    def test_same_number_uses_selected_occurrence_not_all_matches(self):
        result = claim_slice(self.claim(source(BODY.replace("CLP*SECOND", "CLP*FIRST")), sequence=2))
        self.assert_envelope(result, "0002")
        self.assertIn("SECOND*PATIENT", result["content"])
        self.assertNotIn("FIRST*PATIENT", result["content"])

    def test_two_claims_one_transaction_updates_payment_and_excludes_plb(self):
        body = ("ST*835*0001~BPR*I*160*C*CHK~LX*1~CLP*FIRST*1*100*80*0~SVC*HC:111*100*80~"
                "LX*2~CLP*SECOND*1*120*90*0~SVC*HC:222*120*90~PLB*PROVIDER*20261231*WO:REF*10~SE*10*0001~")
        result = claim_slice(self.claim(source(body), "SECOND", 2))
        self.assert_envelope(result, "0001")
        self.assertIn("BPR*I*90.00*C*CHK", result["content"])
        self.assertNotIn("PLB*", result["content"])
        self.assertNotIn("LX*1", result["content"])
        self.assertNotIn("FIRST", result["content"])

    def test_claim_number_in_reference_is_not_a_claim_match(self):
        content = source(BODY.replace("SVC*HC:222", "REF*EA*FIRST~SVC*HC:222"))
        result = claim_slice(self.claim(content))
        self.assert_envelope(result, "0001")
        self.assertNotIn("SECOND", result["content"])

    def test_missing_ambiguous_or_broken_envelope_fails_closed(self):
        for content, number, sequence in ((source(BODY), "ABSENT", 1),
                (source(BODY.replace("CLP*SECOND", "CLP*FIRST")), "FIRST", 99),
                (source(BODY).replace("SE*8*0001", "SE*8*WRONG"), "FIRST", 1)):
            result = claim_slice(self.claim(content, number, sequence))
            self.assertTrue(result["error"])
            self.assertEqual(result["content"], "")

    def test_long_claim_is_not_truncated(self):
        content = source(BODY.replace("SVC*HC:111*100*80**1~", "REF*EA*VALUE~" * 300))
        result = claim_slice(self.claim(content))
        self.assert_envelope(result, "0001")
        self.assertEqual(result["content"].count("REF*EA*VALUE"), 300)

    def test_alternate_separators_are_preserved(self):
        content = source(BODY).replace("*", "|").replace("~", "!")
        result = claim_slice(self.claim(content))
        self.assertEqual(result["error"], "")
        self.assertIn("ST|835|0001!\n", result["content"])
        self.assertNotIn("SECOND", result["content"])

    def test_selected_interchange_controls_are_used(self):
        second = source(BODY).replace("000000001", "000000002").replace("FIRST", "THIRD").replace("SECOND", "FOURTH")
        result = claim_slice(self.claim(source(BODY) + second, "THIRD", 3))
        self.assertEqual(result["error"], "")
        self.assertIn("*000000002*0*T*:~", result["content"])
        self.assertTrue(result["content"].endswith("IEA*1*000000002~\n"))
        self.assertNotIn("000000001", result["content"])

    def test_normalizer_stops_at_transaction_and_group_boundaries(self):
        rows = normalized_835_claims(source(BODY))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["segments"][-1], "SVC*HC:111*100*80**1")
        self.assertEqual(rows[1]["segments"][-1], "SVC*HC:222*120*90**1")
