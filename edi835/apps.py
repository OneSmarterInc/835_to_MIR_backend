from django.apps import AppConfig


class Edi835Config(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "edi835"
    verbose_name = "EDI 835 Module"

    def ready(self):
        # ClaimAlertEmail lives in a focused module to keep the already-large
        # edi835.models module stable while still registering the model with
        # Django's app registry before request/worker code uses it.
        from . import alert_models  # noqa: F401
        from . import signals  # noqa: F401
