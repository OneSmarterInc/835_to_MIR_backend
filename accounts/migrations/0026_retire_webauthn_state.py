from django.db import migrations


class Migration(migrations.Migration):
    # Join both histories: installations before the WebAuthn revert, and
    # installations where 0024/0025 ran after their dependency was rewired.
    dependencies = [
        ("accounts", "0025_demo_role"),
        ("accounts", "0023_merge_20260921_1832"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # Retire the runtime models without destroying existing credential
            # records during a compatibility repair. No endpoint accesses them.
            database_operations=[],
            state_operations=[
                migrations.DeleteModel(name="WebAuthnChallenge"),
                migrations.DeleteModel(name="WebAuthnCredential"),
            ],
        ),
    ]
