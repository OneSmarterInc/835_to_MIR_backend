# 835 to MIR Backend System

## Environment Configuration

In production, platform and process environment variables take precedence over local configuration files (`load_dotenv` adheres to the default `override=False` behavior).

If local development requires clearing or resetting residual audit keys from the current shell environment:

```bash
unset OSIVAULT_AUDIT_CURRENT_KEY
```
