from datetime import datetime
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from .batch_test_837_v3 import _append_hhss as append_837_hhss
from .sftp_automation_operations import _append_hhss as append_outbound_hhss


class OutboundHHSSNamingTests(SimpleTestCase):
    def setUp(self):
        self.when = timezone.make_aware(datetime(2026, 9, 15, 14, 3, 27))

    def test_837_test_relay_appends_hhss_before_extension(self):
        self.assertEqual(
            append_837_hhss("837OUT_20260915.837", now=self.when),
            "837OUT_20260915_1427.837",
        )

    def test_scheduled_837_out_appends_hhss_before_extension(self):
        self.assertEqual(
            append_outbound_hhss("837OUT_20260915.837", now=self.when),
            "837OUT_20260915_1427.837",
        )

    def test_scheduled_mir_out_appends_hhss_before_extension(self):
        self.assertEqual(
            append_outbound_hhss("MIROUT_20260915.MIR", now=self.when),
            "MIROUT_20260915_1427.MIR",
        )
