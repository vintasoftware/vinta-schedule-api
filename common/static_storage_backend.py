from django.conf import settings

from storages.backends.s3boto3 import S3StaticStorage


class StaticStorage(S3StaticStorage):
    bucket_name = getattr(settings, "AWS_STATIC_BUCKET_NAME", "")
    location = getattr(settings, "AWS_STATIC_LOCATION", "")
    custom_domain = getattr(settings, "AWS_STATIC_S3_CUSTOM_DOMAIN", "")
