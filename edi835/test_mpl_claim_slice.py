from types import SimpleNamespace

from django.test import SimpleTestCase

from .mpl_slice_views import _slice_x12
from .mpl_source_fixes import strict_internal_claim_number_from_source


class MPLClaimSliceTests(SimpleTestCase):
    def test_internal_claim_stops_before_packed_following_fields(self):
        highmark = "86520262181153600"
        self.assertEqual(
            strict_internal_claim_number_from_source(
                highmark,
                "QZD934002026081420260814B0000000000",
            ),
            "QZD934",
        )
        self.assertEqual(
            strict_internal_claim_number_from_source(
                highmark,
                f"HI{highmark}QZD934002026081420260814B0000000000",
            ),
            "QZD934",
        )

    def test_835_slice_keeps_x12_envelope_and_only_requested_claim(self):
        content = (
            "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *"
            "260814*1200*^*00501*000000001*0*T*:~"
            "GS*HP*S*R*20260814*1200*1*X*005010X221A1~"
            "ST*835*0480~BPR*I*340*C*CHK************20260814~TRN*1*ABC~LX*1~"
            "CLP*86520262000982500*4*340*0*340*ZZ*QYD579*11*1~"
            "NM1*QC*1*DUKE*DOUGLAS~SVC*HC:99204*340*0**1~"
            "CLP*99920262000982500*1*100*80*0*ZZ*ABC123*11*1~"
            "NM1*QC*1*OTHER*CLAIM~SVC*HC:99213*100*80**1~"
            "SE*12*0480~GE*1*1~IEA*1*000000001~"
        )
        sliced = _slice_x12(content, "835", "86520262000982500", "QYD579")
        self.assertTrue(sliced.startswith("ISA*"))
        self.assertIn("GS*HP*", sliced)
        self.assertIn("ST*835*0480~", sliced)
        self.assertIn("CLP*86520262000982500", sliced)
        self.assertNotIn("CLP*99920262000982500", sliced)
        self.assertIn("SE*", sliced)
        self.assertIn("GE*1*1~", sliced)
        self.assertTrue(sliced.endswith("IEA*1*000000001~"))

    def test_835_slice_does_not_leak_the_next_transaction(self):
        content = (
            "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       *"
            "260814*1200*^*00501*000000001*0*T*:~"
            "GS*HP*S*R*20260814*1200*1*X*005010X221A1~"
            "ST*835*0480~BPR*I*340*C*CHK************20260814~LX*1~"
            "CLP*86520262000982500*4*340*0*340*ZZ*QYD579*11*1~NM1*QC*1*DUKE*DOUGLAS~"
            "SE*6*0480~"
            "ST*835*0481~BPR*I*580*C*CHK************20260814~LX*1~"
            "CLP*11120262000982500*1*580*580*0*ZZ*XYZ111*11*1~NM1*QC*1*SECOND*CLAIM~"
            "SE*6*0481~GE*2*1~IEA*1*000000001~"
        )
        sliced = _slice_x12(content, "835", "86520262000982500", "QYD579")
        self.assertIn("ST*835*0480", sliced)
        self.assertNotIn("ST*835*0481", sliced)
        self.assertNotIn("11120262000982500", sliced)
        self.assertIn("GE*1*1~", sliced)
