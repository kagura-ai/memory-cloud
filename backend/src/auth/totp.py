"""TOTP (Time-based One-Time Password) utilities.

Issue #51: Password + MFA login for initial admin.
"""

import pyotp


def generate_totp_secret() -> str:
    """Generate a new TOTP secret key."""
    return pyotp.random_base32()


def get_provisioning_uri(secret: str, login_id: str, issuer: str = "Kagura Memory Cloud") -> str:
    """Generate a provisioning URI for authenticator apps."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=login_id, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    """Verify a TOTP code (allows 1 period of clock skew)."""
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)
