import tempfile
import unittest
from pathlib import Path

from docker_backuper.utils.archive import (
    calculate_sha256,
    create_archive,
    extract_archive,
    read_manifest,
)


class TestArchive(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_create_and_extract_archive(self):
        # Create test source files
        src_file = self.base_dir / "sample.txt"
        src_file.write_text("Hello Docker Backuper!", encoding="utf-8")

        output_tar = self.base_dir / "test_snapshot.tar.gz"
        manifest = {
            "target_name": "test-app",
            "volumes": ["vol1"],
            "timestamp": "20261105_000000",
        }

        checksum, total_bytes = create_archive(
            source_map={"volumes/sample.txt": src_file},
            output_archive=output_tar,
            metadata=manifest,
        )

        self.assertTrue(output_tar.is_file())
        self.assertGreater(total_bytes, 0)
        self.assertEqual(len(checksum), 64) # Valid SHA-256 length

        # Verify manifest reading
        read_meta = read_manifest(output_tar)
        self.assertEqual(read_meta["target_name"], "test-app")
        self.assertEqual(read_meta["volumes"], ["vol1"])

        # Extract archive
        dest_extract = self.base_dir / "extracted"
        extracted_manifest = extract_archive(
            archive_path=output_tar,
            destination_dir=dest_extract,
            expected_sha256=checksum,
        )

        self.assertEqual(extracted_manifest["target_name"], "test-app")
        extracted_file = dest_extract / "volumes" / "sample.txt"
        self.assertTrue(extracted_file.is_file())
        self.assertEqual(extracted_file.read_text(encoding="utf-8"), "Hello Docker Backuper!")


if __name__ == "__main__":
    unittest.main()
