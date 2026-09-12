from django.test import SimpleTestCase

from .edi835_claim_service import normalized_835_claims


class Normalized835ClaimParserTests(SimpleTestCase):
    def test_keeps_highmark_and_internal_claim_numbers_separate(self):
        content = (
            "ISA*00*~"
            "CLP*86520262000982500*4*340*0*340*ZZ*QYD579*11*1~"
            "NM1*QC*1*DUKE*DOUGLAS~"
            "SVC*HC:99204*340*0~"
            "SVC*HC:99205*20*0~"
            "CLP*89020262161295900*1*185*75.99*0*ZZ*QZG591*22*1~"
            "SVC*HC:77067*26*124~"
        )

        claims = normalized_835_claims(content)

        self.assertEqual(len(claims), 2)
        self.assertEqual(claims[0]["highmark_claim_number"], "86520262000982500")
        self.assertEqual(claims[0]["internal_claim_number"], "QYD579")
        self.assertEqual(claims[0]["service_count"], 2)
        self.assertEqual(claims[1]["highmark_claim_number"], "89020262161295900")
        self.assertEqual(claims[1]["internal_claim_number"], "QZG591")
        self.assertEqual(claims[1]["service_count"], 1)

    def test_does_not_guess_a_numeric_or_highmark_value_as_internal(self):
        claims = normalized_835_claims(
            "CLP*86520262000982500*1*10*5*0*ZZ*86520262000982500*11~"
        )

        self.assertEqual(claims[0]["internal_claim_number"], "")
