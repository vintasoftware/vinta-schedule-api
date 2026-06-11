from decouple import config  # type: ignore

from .production import *


# Staging mirrors production but must never participate in HSTS preload and
# points the account/email flows at the staging frontend.
SECURE_HSTS_PRELOAD = False

FRONTEND_BASE_URL = config(
    "FRONTEND_BASE_URL", default="https://schedule-staging.vintasoftware.com"
).rstrip("/")

HEADLESS_FRONTEND_URLS = {
    "account_confirm_email": f"{FRONTEND_BASE_URL}/verify-email/{{key}}",
    "account_reset_password": f"{FRONTEND_BASE_URL}/password-reset",
    "account_reset_password_from_key": f"{FRONTEND_BASE_URL}/password-reset/{{key}}",
    "account_signup": f"{FRONTEND_BASE_URL}/signup",
    "socialaccount_login_error": f"{FRONTEND_BASE_URL}/social-login-error",
}
