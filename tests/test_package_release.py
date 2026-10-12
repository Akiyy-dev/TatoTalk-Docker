import gzip
import hashlib
import io
import json
import random
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import package_release
except ModuleNotFoundError:
    package_release = None


class ReleasePackagingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(package_release, "The release packager has not been implemented")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = self.root / "image.tar"
        self.output = self.root / "dist"
        self.output.mkdir()
        self.zip_file = self.output / "TatoTalk-Docker.zip"
        self.zip_file.write_bytes(b"deployment archive fixture")
        payload = random.Random(1234).randbytes(5000)
        with tarfile.open(self.image, "w") as archive:
            info = tarfile.TarInfo("image-layer.bin")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    def package(self, part_size=97):
        return package_release.package_release(
            self.image, self.output, "v0.1.0", part_size=part_size
        )

    def artifact_snapshot(self):
        return {path.name: path.read_bytes() for path in self.output.iterdir() if path.is_file()}

    def test_small_parts_reassemble_to_original_tar(self):
        manifest = self.package()
        parts = manifest["parts"]
        self.assertGreater(len(parts), 10)
        self.assertEqual(
            [part["filename"] for part in parts],
            [f"tatotalk-android-amd64.tar.gz.part-{index:04d}" for index in range(1, len(parts) + 1)],
        )
        for index, part in enumerate(parts):
            data = (self.output / part["filename"]).read_bytes()
            self.assertEqual(len(data), part["size"])
            self.assertLessEqual(part["size"], 97)
            self.assertLess(part["size"], 2**31)
            if index < len(parts) - 1:
                self.assertEqual(part["size"], 97)
            self.assertEqual(hashlib.sha256(data).hexdigest(), part["sha256"])
        compressed = b"".join((self.output / part["filename"]).read_bytes() for part in parts)
        self.assertEqual(gzip.decompress(compressed), self.image.read_bytes())
        self.assertEqual(compressed[4:8], b"\0\0\0\0")
        self.assertEqual(manifest["compressed_size"], len(compressed))
        self.assertEqual(manifest["compressed_sha256"], hashlib.sha256(compressed).hexdigest())
        self.assertEqual(manifest["image_tar_sha256"], hashlib.sha256(self.image.read_bytes()).hexdigest())
        self.assertEqual(manifest["image_tar_size"], self.image.stat().st_size)

    def test_manifest_and_checksums_cover_only_release_assets(self):
        (self.output / "keep.txt").write_bytes(b"unrelated")
        manifest = self.package()
        self.assertEqual(manifest["version"], "v0.1.0")
        self.assertEqual(manifest["image_tag"], "tatotalk-android-probe:local")
        self.assertEqual(manifest["compression"], "gzip")
        self.assertEqual(manifest["part_size"], 97)
        self.assertEqual(json.loads((self.output / "image-manifest.json").read_text(encoding="utf-8")), manifest)
        deployment = manifest["deployment_archive"]
        self.assertEqual(deployment["filename"], "TatoTalk-Docker.zip")
        self.assertEqual(deployment["size"], self.zip_file.stat().st_size)
        self.assertEqual(deployment["sha256"], hashlib.sha256(self.zip_file.read_bytes()).hexdigest())
        checksums = (self.output / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
        names = [line.split("  ", 1)[1] for line in checksums]
        self.assertEqual(names, [part["filename"] for part in manifest["parts"]] + ["TatoTalk-Docker.zip", "image-manifest.json"])
        for line in checksums:
            checksum, name = line.split("  ", 1)
            self.assertEqual(checksum, hashlib.sha256((self.output / name).read_bytes()).hexdigest())
        self.assertEqual((self.output / "keep.txt").read_bytes(), b"unrelated")

    def test_compression_is_deterministic_across_source_filenames(self):
        first = self.package()
        first_bytes = b"".join((self.output / part["filename"]).read_bytes() for part in first["parts"])
        other_image = self.root / "other-source.tar"
        other_image.write_bytes(self.image.read_bytes())
        other_output = self.root / "other-dist"
        other_output.mkdir()
        (other_output / "TatoTalk-Docker.zip").write_bytes(self.zip_file.read_bytes())
        second = package_release.package_release(other_image, other_output, "v0.1.0", part_size=97)
        second_bytes = b"".join((other_output / part["filename"]).read_bytes() for part in second["parts"])
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual((self.output / "image-manifest.json").read_bytes(), (other_output / "image-manifest.json").read_bytes())

    def test_missing_source_does_not_create_outputs(self):
        snapshot = self.artifact_snapshot()
        with self.assertRaises(FileNotFoundError):
            package_release.package_release(self.root / "missing.tar", self.output, "v0.1.0", part_size=97)
        self.assertEqual(self.artifact_snapshot(), snapshot)

    def test_missing_deployment_archive_does_not_create_outputs(self):
        self.zip_file.unlink()
        with self.assertRaises(FileNotFoundError):
            self.package()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_any_existing_part_is_rejected_without_changing_files(self):
        (self.output / "tatotalk-android-amd64.tar.gz.part-old").write_bytes(b"old part")
        snapshot = self.artifact_snapshot()
        with self.assertRaises(FileExistsError):
            self.package()
        self.assertEqual(self.artifact_snapshot(), snapshot)

    def test_existing_metadata_is_rejected_without_changing_files(self):
        for filename in ("image-manifest.json", "SHA256SUMS.txt"):
            with self.subTest(filename=filename):
                existing = self.output / filename
                existing.write_bytes(b"old metadata")
                snapshot = self.artifact_snapshot()
                with self.assertRaises(FileExistsError):
                    self.package()
                self.assertEqual(self.artifact_snapshot(), snapshot)
                existing.unlink()

    def test_part_size_must_stay_below_two_gibibytes(self):
        snapshot = self.artifact_snapshot()
        for size in (0, -1, 2**31, 2**31 + 1, 1.5, True):
            with self.subTest(size=size):
                with self.assertRaises(ValueError):
                    self.package(part_size=size)
                self.assertEqual(self.artifact_snapshot(), snapshot)

    def test_input_is_read_in_bounded_chunks(self):
        self.image.write_bytes(random.Random(4321).randbytes(2 * 1024 * 1024 + 5))
        original_open = Path.open
        image = self.image

        class BoundedReader(io.BufferedReader):
            def read(self, size=-1):
                if not 0 < size <= 1024 * 1024:
                    raise AssertionError("Image reads must have a bounded one-MiB maximum")
                return super().read(size)

        def guarded_open(path, mode="r", *args, **kwargs):
            if path == image and mode == "rb":
                return BoundedReader(original_open(path, "rb", buffering=0))
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            manifest = self.package(part_size=1024 * 1024)
        compressed = b"".join((self.output / part["filename"]).read_bytes() for part in manifest["parts"])
        self.assertEqual(gzip.decompress(compressed), self.image.read_bytes())

    def test_write_failure_cleans_only_created_outputs(self):
        (self.output / "keep.txt").write_bytes(b"keep")
        snapshot = self.artifact_snapshot()
        original_open = Path.open

        def failing_open(path, mode="r", *args, **kwargs):
            if path.name == "tatotalk-android-amd64.tar.gz.part-0002" and mode == "xb":
                raise OSError("Simulated full disk")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", failing_open):
            with self.assertRaisesRegex(OSError, "full disk"):
                self.package()
        self.assertEqual(self.artifact_snapshot(), snapshot)

    def test_part_creation_race_preserves_the_conflicting_file(self):
        original_open = Path.open
        conflict_name = "tatotalk-android-amd64.tar.gz.part-0002"

        def racing_open(path, mode="r", *args, **kwargs):
            if path.name == conflict_name and mode == "xb":
                with original_open(path, "wb") as conflicting:
                    conflicting.write(b"created by another process")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", racing_open):
            with self.assertRaises(FileExistsError):
                self.package()
        self.assertEqual(set(self.artifact_snapshot()), {"TatoTalk-Docker.zip", conflict_name})
        self.assertEqual((self.output / conflict_name).read_bytes(), b"created by another process")

    def test_metadata_creation_race_cleans_manifest_but_preserves_conflict(self):
        original_open = Path.open

        def racing_open(path, mode="r", *args, **kwargs):
            if path.name == "SHA256SUMS.txt" and mode == "x":
                with original_open(path, "wb") as conflicting:
                    conflicting.write(b"other checksums")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", racing_open):
            with self.assertRaises(FileExistsError):
                self.package()
        self.assertEqual(set(self.artifact_snapshot()), {"TatoTalk-Docker.zip", "SHA256SUMS.txt"})
        self.assertEqual((self.output / "SHA256SUMS.txt").read_bytes(), b"other checksums")

    def test_cli_uses_default_part_size_and_reports_errors(self):
        script = Path(package_release.__file__).resolve()
        result = subprocess.run(
            [sys.executable, str(script), "--image", str(self.image), "--output-dir", str(self.output), "--version", "v0.1.0"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((self.output / "image-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["part_size"], 1900 * 1024 * 1024)
        self.assertEqual(len(manifest["parts"]), 1)
        failed = subprocess.run(
            [sys.executable, str(script), "--image", str(self.image), "--output-dir", str(self.output), "--version", "v0.1.0"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("error", failed.stderr.lower())
        self.assertNotIn("Traceback", failed.stderr)


if __name__ == "__main__":
    unittest.main()
