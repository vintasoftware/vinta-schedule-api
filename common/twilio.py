from django.conf import settings

from twilio.rest import Client


def get_twilio_client() -> Client:
    """
    Build a Twilio REST client.

    Prefers API Key auth (TWILIO_API_KEY_SID / TWILIO_API_KEY_SECRET) when both are set,
    since auth tokens are not recommended. Falls back to the legacy account SID + auth
    token pair so existing setups keep working.
    """
    api_key_sid = getattr(settings, "TWILIO_API_KEY_SID", None)
    api_key_secret = getattr(settings, "TWILIO_API_KEY_SECRET", None)

    if api_key_sid and api_key_secret:
        return Client(api_key_sid, api_key_secret, settings.TWILIO_ACCOUNT_SID)

    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
