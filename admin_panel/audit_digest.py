import hashlib


def compute_legacy_chain_digest(entry_hashes):
    """SHA-256 over every legacy entry_hash in id order, then the row count, then the last hash."""
    hashes = list(entry_hashes)
    h = hashlib.sha256()
    for eh in hashes:
        h.update((eh or "").encode("utf-8"))
    h.update(str(len(hashes)).encode("utf-8"))
    h.update((hashes[-1] if hashes else "").encode("utf-8"))
    return h.hexdigest(), len(hashes), (hashes[-1] if hashes else "")
