from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User


class UserAdmin(BaseUserAdmin):
    list_display = ("email", "name", "mobile", "totp_enabled", "is_staff", "is_active")
    list_filter = ("totp_enabled", "is_staff", "is_active")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal Info", {"fields": ("name", "mobile")}),
        ("2FA Info", {"fields": ("totp_secret", "totp_enabled", "recovery_codes")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "name", "mobile", "password1", "password2"),
        }),
    )
    search_fields = ("email", "name", "mobile")
    ordering = ("email",)


admin.site.register(User, UserAdmin)
