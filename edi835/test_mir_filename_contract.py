from django.test import SimpleTestCase

from .services import resolve_mir_filename


class MIRFilenameContractTests(SimpleTestCase):
    def test_client_configured_format_is_used(self):
        class Client:
            mir_filename_format = "CLAIMS_YYYYMMDD_hhmmss.MIR"

        from datetime import datetime
        from django.utils import timezone

        fixed_time = timezone.make_aware(datetime(2026, 8, 29, 18, 58, 6))
        self.assertEqual(
            resolve_mir_filename(client=Client(), now=fixed_time),
            "CLAIMS_20260829_185806.MIR",
        )

    def test_default_format_is_stable(self):
        from datetime import datetime
        from django.utils import timezone

        fixed_time = timezone.make_aware(datetime(2026, 8, 29, 18, 58, 6))
        self.assertEqual(
            resolve_mir_filename(client=None, now=fixed_time),
            "MIROUT_2026_0829_.MIR",
        )
