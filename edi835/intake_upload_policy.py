"""Bounded content guard; extensionless partner EDI remains supported."""
from pathlib import PurePath

from .intake_storage import source_path


def check_source(filename, source):
    forbidden = {".exe", ".dll", ".com", ".msi", ".bat", ".cmd", ".ps1", ".sh",
                 ".zip", ".gz", ".tgz", ".tar", ".rar", ".7z", ".jar"}
    with source_path(source.key).open("rb") as handle:
        prefix = handle.read(512)
    signatures = (b"MZ", b"\x7fELF", b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08",
                  b"\x1f\x8b", b"Rar!", b"7z\xbc\xaf\x27\x1c", b"\xd0\xcf\x11\xe0", b"%PDF-")
    if (PurePath(filename).suffix.lower() in forbidden or prefix.startswith(signatures)
            or prefix[257:262] == b"ustar"):
        raise ValueError("Unsupported file content. Upload an uncompressed source file.")
