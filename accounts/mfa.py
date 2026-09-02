import time

from django.contrib.auth.hashers import check_password, make_password


def hash_recovery_codes(codes):
    return [make_password(code) for code in codes]


def consume_recovery_code(user, candidate):
    for index, encoded in enumerate(user.recovery_codes or []):
        if check_password(candidate, encoded):
            remaining = list(user.recovery_codes)
            remaining.pop(index)
            user.recovery_codes = remaining
            user.save(update_fields=["recovery_codes"])
            return True
    return False


def verify_fresh_totp(user, code):
    import pyotp

    counter = int(time.time()) // 30
    if user.last_totp_counter == counter:
        return False
    if not pyotp.TOTP(user.totp_secret).verify(code, valid_window=0):
        return False
    user.last_totp_counter = counter
    user.save(update_fields=["last_totp_counter"])
    return True
