from django.conf import settings
from django.db import models

from common.models import BaseModel


class RefreshToken(BaseModel):
    """
    Model to store refresh tokens for users.
    This model is used to manage refresh tokens for user sessions.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="refresh_tokens",
        verbose_name="User",
    )
    token_hash = models.TextField(verbose_name="Refresh Token Hash")
    expires_at = models.DateTimeField(verbose_name="Expires At")

    user_agent = models.CharField(max_length=255, blank=True, verbose_name="User Agent")
    device_name = models.CharField(max_length=255, blank=True, verbose_name="Device Name")
    device_id = models.CharField(max_length=255, blank=True, verbose_name="Device ID")
    operational_system = models.CharField(
        max_length=255, blank=True, verbose_name="Operating System"
    )
    latitude = models.FloatField(null=True, blank=True, verbose_name="Latitude")
    longitude = models.FloatField(null=True, blank=True, verbose_name="Longitude")

    class Meta:
        verbose_name = "Refresh Token"
        verbose_name_plural = "Refresh Tokens"
