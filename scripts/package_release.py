#!/usr/bin/env python3
"""Stream a Docker image tar into deterministic gzip parts for GitHub Releases.

The output directory must already contain TatoTalk-Docker.zip. No complete
compressed image or other temporary copy is written to disk.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import BinaryIO


DEFAULT_PART_SIZE = 1900 * 1024 * 1024
READ_SIZE = 1024 * 1024
PART_PREFIX = "tatotalk-android-amd64.tar.gz.part-"
DEPLOYMENT_ARCHIVE = "TatoTalk-Docker.zip"
MANIFEST_FILENAME = "image-manifest.json"
CHECKSUMS_FILENAME = "SHA256SUMS.txt"
IMAGE_TAG = "tatotalk-android-probe:local"


class _PartWriter:
    """A gzip output sink that opens each part exclusively and hashes its bytes."""

    def __init__(self, output_dir: Path, part_size: int, created: list[Path]):
        self.output_dir = output_dir
        self.part_size = part_size
        self.created = created
        self.parts: list[dict] = []
        self.stream: BinaryIO | None = None
        self.path: Path | None = None
        self.part_bytes = 0
        self.part_digest = hashlib.sha256()
        self.total_bytes = 0
        self.total_digest = hashlib.sha256()

    def _open_part(self) -> None:
        index = len(self.parts) + 1
        if index > 9999:
            raise ValueError("The image requires more than 9999 four-digit parts")
        path = self.output_dir / f"{PART_PREFIX}{index:04d}"
        stream = path.open("xb")
        self.created.append(path)
        self.stream = stream
        self.path = path
        self.part_bytes = 0
        self.part_digest = hashlib.sha256()

    def _finish_part(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.close()
        finally:
            self.stream = None
        self.parts.append({
            "filename": self.path.name,
            "size": self.part_bytes,
            "sha256": self.part_digest.hexdigest(),
        })

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            if self.stream is None:
                self._open_part()
            count = min(len(view) - offset, self.part_size - self.part_bytes)
            block = view[offset:offset + count]
            written = self.stream.write(block)
            if not written:
                raise OSError("Unable to write compressed image part")
            committed = block[:written]
            self.part_digest.update(committed)
            self.total_digest.update(committed)
            self.part_bytes += written
            self.total_bytes += written
            offset += written
            if self.part_bytes == self.part_size:
                self._finish_part()
        return len(data)

    def finish(self) -> None:
        self._finish_part()

    def abort(self) -> None:
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                pass
            finally:
                self.stream = None


def _file_record(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(READ_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return {"filename": path.name, "size": size, "sha256": digest.hexdigest()}


def _write_text_exclusively(path: Path, content: str, created: list[Path]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as output:
        created.append(path)
        output.write(content)


def package_release(
    image: str | Path,
    output_dir: str | Path,
    version: str,
    *,
    part_size: int = DEFAULT_PART_SIZE,
) -> dict:
    """Create gzip parts, a JSON manifest, and SHA256SUMS without overwriting.

    ``part_size`` is measured in bytes and must be strictly below 2 GiB. On
    failure, only artifacts successfully created by this invocation are removed.
    The existing deployment zip and unrelated output files are preserved.
    """
    if isinstance(part_size, bool) or not isinstance(part_size, int) or not 0 < part_size < 2**31:
        raise ValueError("part_size must be an integer between 1 byte and 2 GiB minus 1 byte")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("version must be a nonempty string")

    image = Path(image)
    output_dir = Path(output_dir)
    if not image.is_file():
        raise FileNotFoundError(f"Image tar does not exist or is not a file: {image}")
    deployment_path = output_dir / DEPLOYMENT_ARCHIVE
    if not deployment_path.is_file():
        raise FileNotFoundError(f"Deployment archive is required: {deployment_path}")
    existing = list(output_dir.glob(f"{PART_PREFIX}*"))
    existing.extend(
        path for name in (MANIFEST_FILENAME, CHECKSUMS_FILENAME)
        if (path := output_dir / name).exists() or path.is_symlink()
    )
    if existing:
        names = ", ".join(sorted(path.name for path in existing))
        raise FileExistsError(f"Refusing to mix with existing release artifacts: {names}")

    # Read the small deployment archive before starting the expensive image pass.
    deployment_record = _file_record(deployment_path)
    created: list[Path] = []
    writer = _PartWriter(output_dir, part_size, created)
    image_digest = hashlib.sha256()
    image_bytes = 0
    try:
        with image.open("rb") as source:
            with gzip.GzipFile(filename="", mode="wb", compresslevel=1, fileobj=writer, mtime=0) as compressed:
                while chunk := source.read(READ_SIZE):
                    compressed.write(chunk)
                    image_digest.update(chunk)
                    image_bytes += len(chunk)
        writer.finish()
        manifest = {
            "version": version,
            "image_tag": IMAGE_TAG,
            "compression": "gzip",
            "part_size": part_size,
            "image_tar_size": image_bytes,
            "image_tar_sha256": image_digest.hexdigest(),
            "compressed_size": writer.total_bytes,
            "compressed_sha256": writer.total_digest.hexdigest(),
            "parts": writer.parts,
            "deployment_archive": deployment_record,
        }
        manifest_path = output_dir / MANIFEST_FILENAME
        _write_text_exclusively(
            manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n", created
        )
        checksum_records = writer.parts + [deployment_record, _file_record(manifest_path)]
        checksums = "".join(f"{record['sha256']}  {record['filename']}\n" for record in checksum_records)
        _write_text_exclusively(output_dir / CHECKSUMS_FILENAME, checksums, created)
        return manifest
    except BaseException as failure:
        writer.abort()
        cleanup_errors = []
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_errors.append(str(path))
        if cleanup_errors:
            raise OSError("Could not clean failed release artifacts: " + ", ".join(cleanup_errors)) from failure
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True, help="Docker image tar exported by buildx")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory containing TatoTalk-Docker.zip")
    parser.add_argument("--version", required=True, help="Release version, for example v0.1.0")
    args = parser.parse_args(argv)
    try:
        manifest = package_release(args.image, args.output_dir, args.version)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        f"Packaged {manifest['image_tar_size']} image bytes into "
        f"{len(manifest['parts'])} gzip part(s), {manifest['compressed_size']} compressed bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
