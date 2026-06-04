import logging

from django.core import management
from django.core.files.base import ContentFile

import requests

from users.models import Profile
from vinta_schedule_api import celery_app


logger = logging.getLogger(__name__)

# Social providers hand us an avatar URL; cap what we'll pull into S3.
PROFILE_PICTURE_MAX_BYTES = 5 * 1024 * 1024
PROFILE_PICTURE_TIMEOUT_SECONDS = 10
_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


@celery_app.task
def clearsessions():
    management.call_command("clearsessions")


@celery_app.task
def download_social_profile_picture(profile_pk, url):
    """Fetch a social provider's avatar URL and store it on the Profile in S3.

    Runs async so a slow/failing image fetch never blocks or breaks the social
    auth flow. No-ops if the profile already has a picture or the URL is gone.
    """
    if not url:
        return

    profile = Profile.objects.filter(pk=profile_pk).first()
    if profile is None or profile.profile_picture:
        return

    try:
        response = requests.get(url, timeout=PROFILE_PICTURE_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException:
        logger.warning("Failed to download social profile picture for profile %s", profile_pk)
        return

    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if content_type not in _CONTENT_TYPE_EXTENSIONS:
        logger.warning(
            "Unexpected content type %r for social profile picture of profile %s",
            content_type,
            profile_pk,
        )
        return

    content = response.content
    if len(content) > PROFILE_PICTURE_MAX_BYTES:
        logger.warning("Social profile picture for profile %s exceeds size cap", profile_pk)
        return

    filename = f"social_{profile_pk}.{_CONTENT_TYPE_EXTENSIONS[content_type]}"
    try:
        profile.profile_picture.save(filename, ContentFile(content), save=True)
    except Exception:
        # Storing the avatar is best-effort: a storage outage / misconfiguration
        # must never break the signup that enqueued this. Under
        # CELERY_TASK_ALWAYS_EAGER the task runs inline in the request, so an
        # unhandled error here would 500 the auth flow.
        logger.exception("Failed to store social profile picture for profile %s", profile_pk)
