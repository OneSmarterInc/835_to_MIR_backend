"""Payment dates attached to each 835 transaction, never inferred from names."""
from datetime import date


def payment_date(fields: list[str]) -> date | None:
    value = fields[16].strip() if len(fields) > 16 else ""
    if len(value) != 8 or not value.isascii() or not value.isdecimal():
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None
