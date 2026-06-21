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
    EXTERNAL_CHANGE_REQUESTED = (
        "calendar.event.external_change_requested",
        "External change requested",
    )
    EXTERNAL_CHANGE_APPROVED = (
        "calendar.event.external_change_approved",
        "External change approved",
    )
    EXTERNAL_CHANGE_REJECTED = (
        "calendar.event.external_change_rejected",
        "External change rejected",
    )
    EXTERNAL_CHANGE_AUTO_UNDONE = (
        "calendar.event.external_change_auto_undone",
        "External change auto-undone",
    )
