from django.test import SimpleTestCase

from edi835.mpl_history_fixes import dedupe_history


class MPLHistoryDeduplicationTests(SimpleTestCase):
    def test_merges_source_and_timeline_occurrence_with_equivalent_timestamp(self):
        rows = dedupe_history([
            {
                "date": "2026-09-16T09:19:52.184000Z",
                "file_type": "837",
                "filename": "IP7A260803I",
                "status": "PROCESSED",
                "event": "Claim found in archived file",
                "internal_claim_number": "",
            },
            {
                "date": "2026-09-16T09:19:52+00:00",
                "file_type": "",
                "filename": "IP7A260803I",
                "status": "processed",
                "event": "837 processed",
                "internal_claim_number": "ABC123",
            },
        ])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_type"], "837")
        self.assertEqual(rows[0]["event"], "837 processed")
        self.assertEqual(rows[0]["internal_claim_number"], "ABC123")

    def test_keeps_different_archived_file_occurrences(self):
        rows = dedupe_history([
            {
                "date": "2026-09-16T09:19:52Z",
                "file_type": "837",
                "filename": "IP7A260803I",
                "status": "PROCESSED",
                "event": "837 processed",
            },
            {
                "date": "2026-09-16T09:20:14Z",
                "file_type": "837",
                "filename": "IP7A260901I",
                "status": "PROCESSED",
                "event": "837 processed",
            },
        ])

        self.assertEqual(len(rows), 2)
