"""
MIR Exporter Module
Handles saving and archiving generated MIR text files using original filename.
"""

import os
from pathlib import Path


def export_mir_file(mir_text, output_dir, mir_filename):
    """
    Exports generated MIR text content to output_dir with the given mir_filename.
    Returns normalized path.
    """
    os.makedirs(output_dir, exist_ok=True)
    if not mir_filename.endswith(".mir"):
        mir_filename = f"{mir_filename}.mir"
    file_path = os.path.join(output_dir, mir_filename)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(mir_text)

    return Path(file_path).as_posix()


def archive_mir_file(mir_text, archive_dir, mir_filename):
    """
    Archives generated MIR text content to archive_dir with the given mir_filename.
    Returns normalized path.
    """
    os.makedirs(archive_dir, exist_ok=True)
    if not mir_filename.endswith(".mir"):
        mir_filename = f"{mir_filename}.mir"
    file_path = os.path.join(archive_dir, mir_filename)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(mir_text)

    return Path(file_path).as_posix()
