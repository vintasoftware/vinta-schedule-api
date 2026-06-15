from decouple import config  # type: ignore

from .production import *


FRONTEND_BASE_URL = config(
    "FRONTEND_BASE_URL", default="https://schedule-staging.vintasoftware.com"
).rstrip("/")

HEADLESS_FRONTEND_URLS = {
    "account_confirm_email": f"{FRONTEND_BASE_URL}/auth/verify-email/{{key}}",
    "account_reset_password": f"{FRONTEND_BASE_URL}/auth/request-password-reset",
    "account_reset_password_from_key": f"{FRONTEND_BASE_URL}/auth/reset-password/{{key}}",
    "account_signup": f"{FRONTEND_BASE_URL}/auth/signup",
    "account_accept_invitation": f"{FRONTEND_BASE_URL}/auth/accept-invite/?token={{token}}",
    "socialaccount_login_error": f"{FRONTEND_BASE_URL}/auth/social-login-error",
}
