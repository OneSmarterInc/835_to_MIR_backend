from django.utils import timezone


def active_client_grant_ids(user):
    """Return client ids currently visible to a delegated administrator."""
    if not user or not user.is_authenticated or not user.is_staff:
        return []
    if user.is_superuser:
        return None
    from .models import AdminClientAccessGrant
    return AdminClientAccessGrant.objects.filter(
        administrator=user,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).values_list("client_id", flat=True)


def has_active_client_grant(user, client_id):
    if not user or not user.is_authenticated or not user.is_staff or not client_id:
        return False
    # Super administrators are system-wide operators. Temporary grants remain
    # mandatory for regular administrators only.
    if user.is_superuser:
        return True
    from .models import AdminClientAccessGrant
    return AdminClientAccessGrant.objects.filter(
        administrator=user,
        client_id=client_id,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).exists()


def can_access_client(user, client_id):
    """Return whether this identity may currently access one tenant."""
    if not user or not user.is_authenticated or not client_id:
        return False
    if getattr(user, "client_id", None):
        return str(user.client_id) == str(client_id)
    if not user.is_staff:
        return False
    return user.is_superuser or has_active_client_grant(user, client_id)


def scope_client_queryset(queryset, user, field="client_id"):
    """Apply the complete client visibility rule to a tenant-owned queryset."""
    if getattr(user, "client_id", None):
        return queryset.filter(**{field: user.client_id})
    if not user or not user.is_authenticated or not user.is_staff:
        return queryset.none()
    if user.is_superuser:
        return queryset
    return queryset.filter(**{f"{field}__in": active_client_grant_ids(user)})
