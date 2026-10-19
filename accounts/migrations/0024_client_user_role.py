from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0022_user_login_lockout_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="client_role",
            field=models.CharField(
                choices=[("CLIENT_ADMIN", "Client Admin"), ("CLIENT_USER", "Client User")],
                default="CLIENT_ADMIN",
                max_length=20,
            ),
        ),
    ]
