import logging
from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.smtp import EmailBackend
from admin_panel.models import ClientSmtpConfig
from admin_panel.smtp_crypto import decrypt_smtp_password
from accounts.models import User
from django.utils.html import strip_tags

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
            logger.error(f"Failed to decrypt SMTP password for {client_smtp_config.client.name}: {e}")

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
    Sends an email using the client's configured SMTP settings.
    If to_emails is not provided, sends to all active users associated with the client.
    """
    try:
        config = ClientSmtpConfig.objects.get(client=client)
    except ClientSmtpConfig.DoesNotExist:
        logger.warning(f"Cannot send email: No SMTP config found for client {client.name}")
        return False
        
    if not to_emails:
        to_emails = get_client_users(client)
        
    if not to_emails:
        logger.warning(f"Cannot send email: No users found for client {client.name}")
        return False
        
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
        logger.error(f"Failed to send email for client {client.name}: {e}")
        return False
