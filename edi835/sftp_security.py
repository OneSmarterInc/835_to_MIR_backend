"""Host identity is verified for every SFTP connection, including test/browse."""
from django.conf import settings


def verified_ssh_client(paramiko):
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    known_hosts = getattr(settings, "SFTP_KNOWN_HOSTS_FILE", "")
    if known_hosts:
        ssh.load_host_keys(known_hosts)
    # Historical trust_unknown_key values deliberately cannot bypass this.
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    return ssh
