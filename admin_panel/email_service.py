import logging
from django.utils import timezone
from django.core.mail import EmailMultiAlternatives
from django.core.mail.backends.smtp import EmailBackend
from admin_panel.models import ClientSmtpConfig
from project835.field_crypto import decrypt_smtp_password
from accounts.models import User
from django.utils.html import escape, strip_tags

logger = logging.getLogger(__name__)


def build_onesmarter_email(subject, html_content, client=None):
    """Wrap every system email in the shared OneSmarter visual identity."""
    safe_subject = escape(str(subject or "OneSmarter Notification"))
    organization = escape(getattr(client, "name", "OneSmarter") or "OneSmarter")
    sent_at = timezone.localtime().strftime("%B %d, %Y at %I:%M %p %Z")
    return f"""<!doctype html>
<html lang="en">
  <body style="margin:0;padding:0;background:#eef3f8;font-family:Arial,Helvetica,sans-serif;color:#172033">
    <div style="display:none;max-height:0;overflow:hidden;color:transparent">{safe_subject}</div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#eef3f8;padding:28px 12px">
      <tr><td align="center">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:680px;background:#ffffff;border:1px solid #d7e0ea;border-radius:14px;overflow:hidden;box-shadow:0 8px 24px rgba(23,32,51,.08)">
          <tr>
            <td style="padding:24px 30px;background:linear-gradient(135deg,#172033 0%,#263b59 68%,#0f766e 100%);color:#ffffff">
              <div style="font-size:12px;font-weight:700;letter-spacing:2.2px;text-transform:uppercase;color:#9fe7dc">OneSmarter</div>
              <div style="font-size:24px;font-weight:700;line-height:1.3;margin-top:8px">{safe_subject}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 30px;background:#f5fafb;border-bottom:1px solid #dce7eb">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                <tr>
                  <td style="font-size:12px;color:#52647b"><strong style="color:#172033">Organization:</strong> {organization}</td>
                  <td align="right" style="font-size:12px;color:#52647b">{sent_at}</td>
                </tr>
              </table>
            </td>
          </tr>
          <tr><td style="padding:30px;font-size:15px;line-height:1.65">{html_content}</td></tr>
          <tr>
            <td style="padding:20px 30px;background:#172033;color:#c9d5e3;font-size:12px;line-height:1.6">
              <strong style="color:#ffffff">OneSmarter Inc.</strong><br/>
              Secure, dependable healthcare data operations.<br/>
              <span style="color:#8fa2b8">This is an automated operational notification. Please retain it for your records.</span>
            </td>
          </tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def _detail_table(rows):
    cells = []
    for label, value in rows:
        cells.append(
            '<tr>'
            f'<td style="padding:10px 12px;border:1px solid #d7e0ea;background:#f6f9fc;font-weight:700;color:#34445a;width:38%">{escape(str(label))}</td>'
            f'<td style="padding:10px 12px;border:1px solid #d7e0ea;color:#172033">{escape(str(value or "—"))}</td>'
            '</tr>'
        )
    return '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:18px 0">' + ''.join(cells) + '</table>'


def _format_schedule_datetime(value, timezone_name):
    if not value:
        return "Not scheduled"
    try:
        from zoneinfo import ZoneInfo
        return timezone.localtime(value, ZoneInfo(timezone_name)).strftime("%B %d, %Y at %I:%M %p %Z")
    except Exception:
        return timezone.localtime(value).strftime("%B %d, %Y at %I:%M %p %Z")


def send_automation_schedule_notice(schedule, created=False):
    """Notify active client users when an automation schedule changes."""
    action = "Created" if created else "Updated"
    state = "Enabled" if schedule.enabled else "Disabled"
    subject = f"Automation Schedule {action} – {schedule.get_automation_type_display()}"
    html = (
        f'<p>Dear {escape(schedule.client.name)} Team,</p>'
        f'<p>Your <strong>{escape(schedule.get_automation_type_display())}</strong> automation schedule has been {action.lower()} by an authorized administrator.</p>'
        + _detail_table([
            ("Automation", schedule.get_automation_type_display()),
            ("Status", state),
            ("Daily run time", schedule.run_time.strftime("%I:%M %p")),
            ("Timezone", schedule.timezone),
            ("Next scheduled run", _format_schedule_datetime(schedule.next_run_at, schedule.timezone)),
        ])
        + '<p>No action is required. Please contact your administrator if this schedule is not expected.</p>'
    )
    return send_client_email(schedule.client, subject, html)


def send_automation_run_notice(run):
    """Send a formal terminal summary for a scheduled automation run."""
    status_colors = {"SUCCESS": ("#0f766e", "#e7f7f3"), "FAILED": ("#b42318", "#fff0ed"), "SKIPPED": ("#9a6700", "#fff7d6")}
    foreground, background = status_colors.get(run.status, ("#34445a", "#eef3f8"))
    subject = f"Automation Run {run.status.title()} – {run.get_automation_type_display()}"
    status_badge = f'<span style="display:inline-block;padding:5px 10px;border-radius:999px;background:{background};color:{foreground};font-size:12px;font-weight:700;letter-spacing:.4px">{escape(run.status)}</span>'
    file_names = list(run.input_835_files or []) + list(run.input_recon_files or [])
    output_names = list(run.mir_output_files or [])
    html = (
        f'<p>Dear {escape(run.client.name)} Team,</p>'
        f'<p>The scheduled <strong>{escape(run.get_automation_type_display())}</strong> operation has reached a final status: {status_badge}</p>'
        + _detail_table([
            ("Scheduled for", _format_schedule_datetime(run.scheduled_for, getattr(run.schedule, "timezone", "UTC"))),
            ("Finished at", _format_schedule_datetime(run.finished_at, getattr(run.schedule, "timezone", "UTC"))),
            ("835 files processed", run.processed_835_count),
            ("837 / RECON files imported", run.recon_file_count),
            ("Input files", ", ".join(str(item) for item in file_names[:10]) or "None"),
            ("MIR output", ", ".join(str(item) for item in output_names[:10]) or "None"),
            ("Result detail", run.error_message or "Completed without a reported error."),
        ])
        + ('<p><strong>Action recommended:</strong> Review the automation run summary and correct the reported condition before the next scheduled run.</p>' if run.status in {"FAILED", "SKIPPED"} else '<p>No action is required. The next run will occur according to the saved schedule.</p>')
    )
    return send_client_email(run.client, subject, html)

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

    html_content = build_onesmarter_email(subject, html_content, client=client)
        
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
