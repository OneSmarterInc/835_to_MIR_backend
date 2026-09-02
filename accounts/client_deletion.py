"""Security boundary for permanent tenant deletion."""

from pathlib import Path

from django.conf import settings
from django.db import transaction

from .models import Client, User


class ClientDeletionError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _local_client_files(client):
    """Collect only paths stored on records owned by this client."""
    paths = set()
    for record in client.edi835_files.only("input_path", "output_path", "archive_path"):
        paths.update(filter(None, (record.input_path, record.output_path, record.archive_path)))
    document_files = [document.file.name for document in client.documents.only("file") if document.file]
    return paths, document_files


def _delete_local_paths(paths):
    base_dir = Path(settings.BASE_DIR).resolve()
    media_root = Path(settings.MEDIA_ROOT).resolve()
    for value in paths:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(media_root)
        except (OSError, ValueError):
            continue
        try:
            if resolved.is_file():
                resolved.unlink()
        except OSError:
            # Database deletion must remain authoritative even if storage is
            # temporarily unavailable. A later storage cleanup can retry.
            pass


def permanently_delete_client(*, actor, client_id, confirmation_name, password):
    """Delete one tenant after strong, server-side superadmin verification."""
    if not actor or not actor.is_authenticated or not actor.is_superuser:
        raise ClientDeletionError("Only a super administrator can permanently delete a client.", 403)

    if not confirmation_name or not password:
        raise ClientDeletionError("Client name confirmation and super administrator password are required.")

    try:
        client = Client.objects.get(id=client_id)
    except (Client.DoesNotExist, ValueError):
        raise ClientDeletionError("Client not found.", 404)

    if confirmation_name != client.name:
        raise ClientDeletionError("Client name does not match. Enter the exact client name.")

    if not actor.check_password(password):
        raise ClientDeletionError("Super administrator password is incorrect.", 403)

    if actor.client_id == client.id:
        raise ClientDeletionError("You cannot delete the client linked to your own super administrator account.", 409)

    local_paths, document_files = _local_client_files(client)
    document_storage = None
    first_document = client.documents.only("file").first()
    if first_document:
        document_storage = first_document.file.storage

    name = client.name
    with transaction.atomic():
        # Client users use SET_NULL so they must be explicitly removed. Never
        # silently delete another super administrator account.
        linked_users = User.objects.filter(client=client)
        if linked_users.filter(is_superuser=True).exists():
            raise ClientDeletionError(
                "Remove the client assignment from its super administrator before deleting this client.",
                409,
            )
        linked_users.delete()

        # Audit history is retained. The FK's SET_NULL operation removes only
        # the live tenant reference while preserving the immutable event.
        client.delete()

        def cleanup_storage():
            _delete_local_paths(local_paths)
            if document_storage:
                for stored_name in document_files:
                    try:
                        document_storage.delete(stored_name)
                    except OSError:
                        pass

        transaction.on_commit(cleanup_storage)

    return name
