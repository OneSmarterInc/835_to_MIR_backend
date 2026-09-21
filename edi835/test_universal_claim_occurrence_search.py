from datetime import timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from .universal_claim_occurrence_search import _assemble_highmark_rows, _serialize_row


def occurrence(source_type, claim_id, when, internal=""):
    return {
        "claim_id": str(claim_id),
        "claim_sequence": int(claim_id),
        "highmark": "0402026200000001",
        "internal": internal,
        "file_id": f"file-{source_type}-{claim_id}",
        "filename": f"{source_type}-{claim_id}.dat",
        "status": "PROCESSED",
        "arrived_at": when,
    }


class UniversalClaimOccurrencePairingTests(SimpleTestCase):
    def test_837_occurrences_remain_separate_and_835_enriches_first_occurrence(self):
        base = timezone.now()
        sources = {
            "837": [occurrence("837", i, base + timedelta(minutes=i)) for i in range(1, 5)],
            "835": [occurrence("835", 10, base + timedelta(minutes=10), "QNZ911")],
            "mir": [occurrence("mir", 20, base + timedelta(minutes=20), "QNZ911")],
            "recon": [occurrence("recon", 30, base + timedelta(minutes=30), "QNZ911")],
        }

        rows = _assemble_highmark_rows("0402026200000001", sources)

        self.assertEqual(len(rows), 4)
        self.assertEqual([row["837"]["claim_id"] for row in rows], ["1", "2", "3", "4"])
        self.assertEqual(rows[0]["835"]["claim_id"], "10")
        self.assertEqual(rows[0]["internal"], "QNZ911")
        self.assertEqual(rows[0]["mir"]["claim_id"], "20")
        self.assertEqual(rows[0]["recon"]["claim_id"], "30")
        for row in rows[1:]:
            self.assertIsNone(row["835"])
            self.assertEqual(row["internal"], "")

        serialized = [_serialize_row(row) for row in rows]
        self.assertEqual(serialized[0]["internal_claim_number"], "QNZ911")
        self.assertEqual(serialized[0]["lifecycle"]["837"]["internal_claim_number"], "")
        self.assertEqual(serialized[1]["internal_claim_number"], "")

    def test_multiple_835_occurrences_pair_oldest_to_oldest_837(self):
        base = timezone.now()
        sources = {
            "837": [
                occurrence("837", 1, base + timedelta(minutes=1)),
                occurrence("837", 2, base + timedelta(minutes=2)),
            ],
            "835": [
                occurrence("835", 11, base + timedelta(minutes=11), "AAA111"),
                occurrence("835", 12, base + timedelta(minutes=12), "BBB222"),
            ],
            "mir": [],
            "recon": [],
        }

        rows = _assemble_highmark_rows("0402026200000001", sources)

        self.assertEqual(rows[0]["837"]["claim_id"], "1")
        self.assertEqual(rows[0]["internal"], "AAA111")
        self.assertEqual(rows[1]["837"]["claim_id"], "2")
        self.assertEqual(rows[1]["internal"], "BBB222")
