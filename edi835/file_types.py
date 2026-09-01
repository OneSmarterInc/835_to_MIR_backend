import os


FILE_EXTENSION_POLICIES = {
    "835": (".835", ".x12", ".edi", ".txt", ".dat", ".35", ".ansi", ".rem"),
    "837": (".837", ".x12", ".edi", ".txt", ".dat"),
    "RECON": (".csv", ".tsv", ".txt", ".dat", ".p7a", ".recon", ".out", ".mir"),
}


def allowed_extensions(kind):
    return FILE_EXTENSION_POLICIES[str(kind).upper()]


def file_extension_error(kind):
    label = str(kind).upper()
    return f"Wrong file format for {label}. Allowed extensions: {', '.join(allowed_extensions(label))}."


def has_valid_file_extension(filename, kind):
    extension = os.path.splitext(os.path.basename(str(filename or "")))[1].lower()
    return bool(extension and extension in allowed_extensions(kind))


def validate_file_extension(filename, kind):
    if not has_valid_file_extension(filename, kind):
        raise ValueError(file_extension_error(kind))
    return True
