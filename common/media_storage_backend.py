from django.conf import settings

from storages.backends.s3boto3 import S3Boto3Storage


class MediaStorage(S3Boto3Storage):
    bucket_name = getattr(settings, "AWS_MEDIA_BUCKET_NAME", "")
    location = getattr(settings, "AWS_MEDIA_LOCATION", "")

    if getattr(settings, "USE_LOCALSTACK", False):
        endpoint_url = getattr(settings, "LOCALSTACK_ENDPOINT", "")
    elif getattr(settings, "USE_MINIO", False):
        endpoint_url = getattr(settings, "MINIO_ENDPOINT", "")
    else:
        custom_domain = getattr(settings, "AWS_MEDIA_S3_CUSTOM_DOMAIN", "")
