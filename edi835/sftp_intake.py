"""Stable remote reads, cross-adapter ownership, and receipt-gated deletion.

Partners must publish immutable final names (upload .part, then rename). Repeated
observations protect older senders; a paused writer cannot be proven complete by
SFTP stat alone. Uncertain post-acceptance work is retained for operator review.
"""
import hashlib
import json
import logging
import posixpath
import stat
import threading
import uuid
from contextvars import ContextVar
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from .models import SFTPSourceClaim


logger = logging.getLogger(__name__)

_session = ContextVar("sftp_intake_session", default=None)


class SourceNotReady(ValueError):
    pass


def lease_seconds():
    return max(30, int(getattr(settings, "SFTP_SOURCE_LEASE_SECONDS", 300)))


def intake_session(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _session.get() is not None:
            return function(*args, **kwargs)
        owner = uuid.uuid4()
        token = _session.set(owner)
        stop = threading.Event()

        def heartbeat():
            close_old_connections()
            try:
                while not stop.wait(lease_seconds() / 3):
                    now = timezone.now()
                    SFTPSourceClaim.objects.filter(owner=owner, lease_expires_at__gt=now).update(
                        lease_expires_at=now + timedelta(seconds=lease_seconds()))
            except Exception:
                logger.exception("SFTP source lease renewal failed; expired owners are fenced")
            finally:
                close_old_connections()

        worker = None
        if not connection.in_atomic_block:
            worker = threading.Thread(target=heartbeat, daemon=True)
            worker.start()
        try:
            return function(*args, **kwargs)
        finally:
            stop.set()
            if worker:
                worker.join(timeout=2)
            # Never automatically replay a source once durable acceptance or
            # external side effects may have occurred. Expiry follows this rule too.
            SFTPSourceClaim.objects.filter(owner=owner, state="READING", storage_key="").update(
                owner=None, lease_expires_at=None, state="OBSERVING")
            SFTPSourceClaim.objects.filter(owner=owner, state="STORED",
                receipt__workflow="VALIDATE_835", receipt__state="COMPLETED").update(
                    state="ACCEPTED", owner=None, lease_expires_at=None)
            SFTPSourceClaim.objects.filter(owner=owner, state="STORED").update(
                state="REVIEW", owner=None, lease_expires_at=None)
            _session.reset(token)
    return wrapped


def _metadata(attributes):
    if not stat.S_ISREG(attributes.st_mode or 0):
        raise SourceNotReady("Only regular remote files can be ingested.")
    if attributes.st_size is None or attributes.st_mtime is None or attributes.st_size <= 0:
        raise SourceNotReady("Remote size and modification time are required.")
    return int(attributes.st_size), int(attributes.st_mtime)


class GuardedSFTP:
    """Inbound-only adapter. Outbound uploads and browsing use ordinary SFTP."""

    def __init__(self, sftp, credentials, client):
        if _session.get() is None:
            raise RuntimeError("SFTP intake requires an ownership session.")
        self.raw = sftp
        self.client_id = getattr(client, "pk", None)
        self.endpoint = [str(credentials["host"]).lower().rstrip("."),
                         int(credentials.get("port") or 22), str(credentials["username"])]
        self.owner = _session.get()

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def _identity(self, path):
        canonical = self.raw.normalize(path)
        return hashlib.sha256(json.dumps([*self.endpoint, canonical]).encode()).hexdigest(), canonical

    def _observe(self, path, attributes, *, acquire=False):
        size, mtime = _metadata(attributes)
        identity, canonical = self._identity(path)
        now = timezone.now()
        with transaction.atomic():
            claim, _ = SFTPSourceClaim.objects.get_or_create(identity=identity, defaults={
                "client_id": self.client_id, "remote_path": canonical, "size": size, "mtime": mtime})
            claim = SFTPSourceClaim.objects.select_for_update().get(pk=identity)
            if claim.client_id != self.client_id:
                raise SourceNotReady("Remote path is assigned to another client; review route configuration.")
            active = claim.owner and claim.lease_expires_at and claim.lease_expires_at > now
            if active or (claim.owner and claim.state in {"STORED", "REVIEW"}):
                return None
            if (claim.size, claim.mtime) != (size, mtime) or claim.state == "REMOVED":
                claim.size, claim.mtime, claim.stable_since = size, mtime, now
                claim.state, claim.owner, claim.receipt, claim.storage_key = "OBSERVING", None, None, ""
                claim.save()
                return None
            if claim.state in {"STORED", "REVIEW", "ACCEPTED"}:
                return None
            interval = max(1, int(getattr(settings, "SFTP_STABILITY_SECONDS", 30)))
            if (now - claim.stable_since).total_seconds() < interval:
                return None
            if acquire:
                claim.owner, claim.state = self.owner, "READING"
                claim.lease_expires_at = now + timedelta(seconds=lease_seconds())
                claim.save()
            return claim

    def listdir_attr(self, folder):
        ready = []
        for entry in self.raw.listdir_attr(folder):
            name = entry.filename
            if (name.startswith(".") or posixpath.basename(name) != name
                    or name.lower().endswith((".part", ".tmp", ".uploading", ".filepart"))):
                continue
            if not stat.S_ISREG(entry.st_mode or 0):
                continue
            if not getattr(entry, "st_size", None) or getattr(entry, "st_mtime", None) is None:
                # An empty/incomplete neighbor must not block ready files.
                continue
            if self._observe(posixpath.join(folder, name), entry):
                ready.append(entry)
        return ready

    def open(self, path, mode="rb"):
        if mode != "rb":
            raise ValueError("Inbound intake is read-only until safe disposition.")
        if posixpath.basename(path).startswith(".") or path.lower().endswith((".part", ".tmp", ".uploading", ".filepart")):
            raise SourceNotReady("Upload has not been published under its final filename.")
        claim = self._observe(path, self.raw.lstat(path), acquire=True)
        if claim is None:
            raise SourceNotReady("Source is still stabilizing, already claimed, or awaiting review; retained on SFTP.")
        return _SourceReader(self, path, claim, self.raw.open(path, "rb"))

    def remove(self, path):
        identity, _ = self._identity(path)
        # Lock across verification and unlink to fence concurrent application pollers.
        with transaction.atomic():
            claim = SFTPSourceClaim.objects.select_for_update().get(pk=identity)
            self._assert_owner(claim)
            receipt = claim.receipt
            if receipt and receipt.canonical_transfer_id:
                raise SourceNotReady("Duplicate remote source retained; review its canonical receipt before disposition.")
            if not receipt or receipt.state not in {"COMPLETED", "DUPLICATE"}:
                raise SourceNotReady("Remote source retained: no successful durable receipt.")
            if receipt.workflow == "VALIDATE_835":
                raise SourceNotReady("Validation alone does not permit remote source deletion.")
            if receipt.workflow == "835" and not receipt.dispatch.outputs.filter(delivery_state="CONFIRMED").exists():
                raise SourceNotReady("Remote source retained: MIR delivery is not confirmed.")
            from .intake_storage import read_verified
            from .intake_outputs import verify_dispatch_outputs
            verify_dispatch_outputs(receipt.dispatch_id)
            read_verified(receipt)
            before = _metadata(self.raw.lstat(path))
            digest, size = hashlib.sha256(), 0
            with self.raw.open(path, "rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    size += len(chunk)
                    if size > receipt.byte_count:
                        raise SourceNotReady("Remote source changed; retained for review.")
                    digest.update(chunk)
            if (before != (claim.size, claim.mtime) or before != _metadata(self.raw.lstat(path))
                    or size != receipt.byte_count or digest.hexdigest() != receipt.checksum):
                raise SourceNotReady("Remote source changed; retained for review.")
            self._assert_owner(claim)
            self.raw.remove(path)
            claim.state = "REMOVED"
            claim.owner, claim.lease_expires_at = None, None
            claim.save()

    def _assert_owner(self, claim):
        if claim.owner != self.owner or not claim.lease_expires_at or claim.lease_expires_at <= timezone.now():
            raise SourceNotReady("SFTP source lease lost; source retained.")


class _SourceReader:
    def __init__(self, guard, path, claim, handle):
        self.guard, self.path, self.claim, self.handle = guard, path, claim, handle
        self.total = 0
        self.complete = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.handle.close()

    def read(self, size=-1):
        data = self.handle.read(size)
        self.total += len(data)
        if not data:
            self.claim.refresh_from_db()
            self.guard._assert_owner(self.claim)
            if (self.total != self.claim.size
                    or _metadata(self.guard.raw.lstat(self.path)) != (self.claim.size, self.claim.mtime)):
                raise SourceNotReady("Remote source changed during download; no receipt accepted.")
            self.complete = True
        return data

    def intake_stored(self, source):
        if not self.complete:
            raise SourceNotReady("Remote read did not complete.")
        with transaction.atomic():
            claim = SFTPSourceClaim.objects.select_for_update().get(pk=self.claim.pk)
            self.guard._assert_owner(claim)
            claim.storage_key, claim.state = source.key, "STORED"
            claim.save()


def bind_receipts(receipts):
    """Called within registration's transaction; source ownership cannot be skipped."""
    for receipt in receipts:
        if receipt.channel != "SFTP":
            continue
        claim = SFTPSourceClaim.objects.select_for_update().filter(storage_key=receipt.storage_key).first()
        if claim:
            if (claim.owner != _session.get() or claim.client_id != receipt.client_id
                    or claim.lease_expires_at <= timezone.now() or claim.receipt_id):
                raise SourceNotReady("SFTP source ownership changed before durable acceptance.")
            claim.receipt = receipt
            claim.save(update_fields=["receipt", "updated_at"])
