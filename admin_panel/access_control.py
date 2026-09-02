from django.utils import timezone


def has_active_client_grant(user, client_id):
    if not user or not user.is_authenticated or not user.is_staff or not client_id:
        return False
    # Super administrators are system-wide operators. Temporary grants remain
    # mandatory for regular administrators only.
    if user.is_superuser:
        return True
    from .models import AdminClientAccessGrant
    return AdminClientAccessGrant.objects.filter(
        administrator=user, client_id=client_id, revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).exists()
