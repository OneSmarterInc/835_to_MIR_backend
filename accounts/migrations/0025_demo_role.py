from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0024_client_user_role")]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="client_role",
            field=models.CharField(
                choices=[("CLIENT_ADMIN", "Client Admin"), ("CLIENT_USER", "Client User"), ("DEMO", "Demo")],
                default="CLIENT_ADMIN",
                max_length=20,
            ),
        ),
    ]
