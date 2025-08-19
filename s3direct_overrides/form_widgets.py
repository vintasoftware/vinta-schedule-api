import os
from urllib.parse import unquote

from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.safestring import mark_safe

from s3direct.widgets import S3DirectWidget as OriginalS3DirectWidget


class S3DirectWidget(OriginalS3DirectWidget):
    class Media(OriginalS3DirectWidget.Media):
        js = ("s3direct/dist/index.js",)
        css = {"all": ("s3direct/dist/index.css",)}  # noqa: RUF012

    def render(self, name, value, **kwargs):
        csrf_cookie_name = getattr(settings, "CSRF_COOKIE_NAME", "csrftoken")

        ctx = {
            "policy_url": reverse("s3direct"),
            "signing_url": reverse("s3direct-signing"),
            "dest": self.dest,
            "name": name,
            "csrf_cookie_name": csrf_cookie_name,
            "signed_url": value.url if value else None,
            "file_url": str(value) if value else None,
            "file_name": os.path.basename(unquote(str(value))) if value else None,
        }

        return mark_safe(  # nosec - we trust the template  # noqa: S308
            render_to_string(os.path.join("s3direct", "s3direct-widget-override.tpl"), ctx)
        )
