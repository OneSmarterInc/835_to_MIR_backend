from django.db import migrations


def add_search_access(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.filter(is_staff=True, is_superuser=False).iterator():
        screens = list(user.admin_screens or [])
        if "conversions" in screens and "search" not in screens:
            position = screens.index("conversions") + 1
            screens.insert(position, "search")
            user.admin_screens = screens
            user.save(update_fields=["admin_screens"])


def remove_search_access(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    for user in User.objects.filter(is_staff=True, is_superuser=False).iterator():
        screens = [screen for screen in (user.admin_screens or []) if screen != "search"]
        user.admin_screens = screens
        user.save(update_fields=["admin_screens"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0018_user_admin_screens")]
    operations = [migrations.RunPython(add_search_access, remove_search_access)]
