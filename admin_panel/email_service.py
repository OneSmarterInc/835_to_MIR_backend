import logging
from django.utils import timezone
from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.smtp import EmailBackend
from admin_panel.models import ClientSmtpConfig
from project835.field_crypto import decrypt_smtp_password
from accounts.models import User
from django.utils.html import escape, strip_tags

logger = logging.getLogger(__name__)

def get_client_users(client):
    """Return a list of user emails belonging to the client."""
    users = User.objects.filter(client=client, is_active=True)
    return [u.email for u in users if u.email]

def get_client_email_backend(client_smtp_config):
    """Returns a configured Django EmailBackend for the client's SMTP config."""
    password = ''
    if client_smtp_config.smtp_password:
        try:
            password = decrypt_smtp_password(client_smtp_config.smtp_password)
        except Exception as e:
            logger.error(f"Failed to decrypt SMTP password for {client_smtp_config.client.name if client_smtp_config.client else 'Default/Global'}: {e}")

    use_tls = client_smtp_config.security == 'STARTTLS'
    use_ssl = client_smtp_config.security == 'SSL_TLS'
    
    return EmailBackend(
        host=client_smtp_config.smtp_host,
        port=client_smtp_config.smtp_port,
        username=client_smtp_config.smtp_username,
        password=password,
        use_tls=use_tls,
        use_ssl=use_ssl,
        fail_silently=False,
    )


def send_client_email(client, subject, html_content, to_emails=None):
    """
    Sends an email using the client's configured SMTP settings,
    or falls back to the default config if 'use_default' is enabled or client has no config.
    """
    config = None
    if client:
        try:
            config = ClientSmtpConfig.objects.get(client=client)
            if config.use_default:
                config = ClientSmtpConfig.objects.filter(client__isnull=True).first()
        except ClientSmtpConfig.DoesNotExist:
            config = ClientSmtpConfig.objects.filter(client__isnull=True).first()
    else:
        config = ClientSmtpConfig.objects.filter(client__isnull=True).first()

    if not config:
        logger.warning(f"Cannot send email: No SMTP configuration found (client: {client.name if client else 'None'}).")
        return False
        
    if not to_emails:
        if client:
            to_emails = get_client_users(client)
        else:
            to_emails = []
            
    if not to_emails:
        logger.warning("Cannot send email: No recipients specified.")
        return False

    # Standardize signature
    if "OneSmarter" not in html_content:
        html_content += "<br/><p>Sincerely,<br/>OneSmarter Inc, USA</p>"
        
    try:
        backend = get_client_email_backend(config)
        
        # Determine sender string "Name <email>"
        from_email = config.sender_email
        if config.sender_name:
            from_email = f"{config.sender_name} <{config.sender_email}>"
            
        headers = {}
        if config.reply_to:
            headers['Reply-To'] = config.reply_to
            
        # We also need a plain text version
        text_content = strip_tags(html_content)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=from_email,
            to=to_emails,
            headers=headers,
            connection=backend
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False


def send_client_offboarding_notice(client, recipient_emails):
    """Send the final access-revocation notice privately to each client user."""
    recipients = sorted({email.strip() for email in recipient_emails if email and email.strip()})
    effective_at = timezone.localtime().strftime("%B %d, %Y at %I:%M %p %Z")
    client_name = escape(client.name)
    subject = "Important: Your OneSmarter access has been discontinued"
    html_content = f"""
        <div style="font-family:Arial,sans-serif;color:#172033;line-height:1.6;max-width:640px">
          <h2 style="color:#172033;margin-bottom:16px">OneSmarter Access Notice</h2>
          <p>Hello,</p>
          <p>
            This message confirms that <strong>{client_name}</strong>'s access to the
            OneSmarter MIR Relay platform was discontinued effective
            <strong>{effective_at}</strong>.
          </p>
          <p>
            Your OneSmarter user account has been deactivated and all active sessions
            have been revoked. You will no longer be able to sign in or access client data.
          </p>
          <p>No action is required from you.</p>
          <p>
            If you believe you received this notice in error or require assistance,
            please contact your organization administrator or OneSmarter Support.
          </p>
          <p style="margin-top:24px">Sincerely,<br/><strong>OneSmarter Inc, USA</strong></p>
          <hr style="border:0;border-top:1px solid #d7dee8;margin:24px 0"/>
          <p style="font-size:12px;color:#64748b">
            This is an administrative security notification concerning your account.
          </p>
        </div>
    """

    sent = 0
    failed = []
    for recipient in recipients:
        # Send one message per user to avoid exposing client user addresses.
        if send_client_email(client, subject, html_content, to_emails=[recipient]):
            sent += 1
        else:
            failed.append(recipient)
    return {"attempted": len(recipients), "sent": sent, "failed": failed}
