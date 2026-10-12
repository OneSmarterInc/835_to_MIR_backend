from django.apps import AppConfig


class AdminPanelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "admin_panel"

    def ready(self):
        try:
            from osivault.audit.signals import register_audit_signal
            from admin_panel.models import (
                MIRMasterAuditLog,
                ClientDocument,
                ClientStepStatus,
                ClientGoLiveStatus,
                ClientOffboardingStatus,
                AdminClientAccessGrant,
                ClientSmtpConfig,
            )
            from accounts.models import User, Client, ClientContact, ClientStepComment
            from edi835.models import SFTPConfig

            # Register automatic audit signals for critical governance models
            models_to_audit = [
                User,
                Client,
                SFTPConfig,
                ClientDocument,
                ClientStepStatus,
                ClientGoLiveStatus,
                ClientOffboardingStatus,
                AdminClientAccessGrant,
                ClientSmtpConfig,
                ClientContact,
                ClientStepComment,
            ]
            for model_cls in models_to_audit:
                register_audit_signal(
                    target_model_class=model_cls,
                    audit_model_class=MIRMasterAuditLog,
                )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Could not register audit signals: {e}")

