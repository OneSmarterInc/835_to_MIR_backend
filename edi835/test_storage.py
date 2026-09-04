import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from .storage import (
    archive_inbound,
    client_storage_dirs,
    remove_delivered_outbound,
    stage_inbound,
    write_mir_copies,
)


class ClientStorageLifecycleTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.temporary.name)
        self.settings_override.enable()
        self.client = SimpleNamespace(id=uuid.uuid4())

    def tearDown(self):
        self.settings_override.disable()
        self.temporary.cleanup()

    def test_exact_tree_is_created_for_each_client(self):
        dirs = client_storage_dirs(self.client)
        expected = {
            "documents", "837_in", "837_archive", "837_out", "835_in",
            "835_archive", "mir_archive", "mir_out", "recon_in",
            "recon_archive",
        }
        self.assertTrue(expected.issubset(dirs))
        self.assertTrue(all(dirs[key].is_dir() for key in expected))
        self.assertIn(str(self.client.id), str(dirs["root"]))

    def test_inbound_is_moved_to_archive_without_overwrite(self):
        first = stage_inbound(self.client, "835", "same.835", "first")
        first_archive = archive_inbound(self.client, "835", first)
        second = stage_inbound(self.client, "835", "same.835", "second")
        second_archive = archive_inbound(self.client, "835", second)

        self.assertFalse(any(client_storage_dirs(self.client)["835_in"].iterdir()))
        self.assertNotEqual(first_archive, second_archive)
        self.assertEqual(first_archive.read_text(), "first")
        self.assertEqual(second_archive.read_text(), "second")

    def test_mir_out_is_deleted_only_after_delivery(self):
        archive, outbound = write_mir_copies(self.client, "result.MIR", "data")
        self.assertTrue(archive.is_file())
        self.assertTrue(outbound.is_file())

        remove_delivered_outbound(self.client, "mir", outbound)
        self.assertTrue(archive.is_file())
        self.assertFalse(outbound.exists())

        with self.assertRaises(ValueError):
            remove_delivered_outbound(self.client, "mir", Path(self.temporary.name) / "elsewhere.MIR")
