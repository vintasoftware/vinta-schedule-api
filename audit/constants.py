from django.db import models


class AuditActorType(models.TextChoices):
    """Type of actor that triggered an audit record."""

    SYSTEM = "system", "System"
    MEMBERSHIP = "membership", "Membership"
    SYSTEM_USER = "system_user", "System user"
    SINGLE_USE_CODE = "single_use_code", "Single-use code"


class AuditAction(models.TextChoices):
    """Action recorded in audit trail.

    Central, extensible enum. Owning modules add members as they instrument
    call sites (e.g. "calendar.event.reschedule"). Seed with generic verbs
    for basic usability on day one.
    """

    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    DELETE = "delete", "Delete"
