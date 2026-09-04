ADMIN_SCREENS = (
    "clients", "onboard", "docs", "conversions", "search", "checks", "result",
    "sftp-automation", "files", "code-dictionary", "promote", "trust",
    "access", "defaults", "audit", "ops", "offboard",
)

DEFAULT_ADMIN_SCREENS = (
    "clients", "onboard", "conversions", "search", "files", "promote", "trust", "ops",
)

SCREEN_LABELS = {
    "clients": "All Clients", "onboard": "Onboarding", "docs": "Documents",
    "conversions": "Conversions", "search": "Search", "checks": "Checks", "result": "Result",
    "sftp-automation": "SFTP Automation", "files": "Archive",
    "code-dictionary": "Code Dictionary", "promote": "Go Live",
    "trust": "Trust Center", "access": "Access", "defaults": "Default Configs",
    "audit": "Audit Log", "ops": "Operations", "offboard": "Offboarding",
}


def normalize_admin_screens(values):
    requested = set(values or ())
    return [screen for screen in ADMIN_SCREENS if screen in requested]


def screens_for_user(user):
    if getattr(user, "is_superuser", False):
        return list(ADMIN_SCREENS)
    if not getattr(user, "is_staff", False):
        return []
    configured = getattr(user, "admin_screens", None)
    return normalize_admin_screens(
        DEFAULT_ADMIN_SCREENS if configured is None else configured
    )


def user_can_access_screen(user, screen):
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_staff", False)
        and (getattr(user, "is_superuser", False) or screen in screens_for_user(user))
    )
