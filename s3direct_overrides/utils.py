from collections.abc import Callable
from urllib.parse import urlparse, urlsplit

from django.conf import settings

from cuid2 import cuid_wrapper


def generate_unique_id():
    cuid_generator: Callable[[], str] = cuid_wrapper()
    return cuid_generator()


def get_signed_url(value):
    from common.media_storage_backend import (
        MediaStorage,
    )

    if not isinstance(value, str):
        return value.url

    if value.startswith(settings.S3DIRECT_ENDPOINT):
        s3_base_url = f"{settings.S3DIRECT_ENDPOINT}/{settings.AWS_MEDIA_BUCKET_NAME}/"
        s3_key = value.split(s3_base_url)[-1]
    elif value.startswith(settings.MEDIA_URL):
        s3_key = value.split(settings.MEDIA_URL)[-1]
    else:
        url_parts = urlsplit(value)
        # url_parts.path starts with a slash
        url_path_parts = url_parts.path.split("/")
        if url_path_parts[0] == settings.AWS_MEDIA_BUCKET_NAME:
            # the first element is an empty string
            # the second is the bucket name
            s3_key = "/".join(url_path_parts[2:])
        else:
            # the first element is an empty string
            s3_key = "/" + "/".join(url_path_parts[1:])
    return MediaStorage().url(name=s3_key, expire=7200)


def adjust_s3_media_url(data):
    if not data:
        return None

    try:
        parsed_url = urlparse(data)
        url = f"{parsed_url.scheme}://{parsed_url.hostname}{parsed_url.path}"
    except Exception:  # noqa: BLE001
        return None
    if url.startswith(settings.S3DIRECT_ENDPOINT):
        s3_base_url = f"{settings.S3DIRECT_ENDPOINT}/{settings.AWS_MEDIA_BUCKET_NAME}/"
        s3_key = url.split(s3_base_url)[-1]
        return f"{settings.MEDIA_URL}{s3_key}"
    if url.startswith(settings.MEDIA_URL):
        return url
    return None
