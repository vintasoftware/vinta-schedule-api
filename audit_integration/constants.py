"""This project's audit vocabulary.

``vinta_audit_logs`` ships create / update / delete and nothing else, on purpose:
the set of things a project audits is the project's business. These are ours.
"""

from django.db import models


class AuditActorType(models.TextChoices):
    """The kinds of principal that act in this project.

    Wider than ``vinta_audit_logs.IdentityType`` because this project
    authenticates more than users: API tokens (``SYSTEM_USER``) and single-use
    calendar management codes both take auditable actions, and the trail has to
    tell them apart from a person.
    """

    SYSTEM = "system", "System"
    MEMBERSHIP = "membership", "Membership"
    SYSTEM_USER = "system_user", "System user"
    SINGLE_USE_CODE = "single_use_code", "Single-use code"


class AuditAction(models.TextChoices):
    """Actions this project records.

    Owning modules add members as they instrument call sites. The generic verbs
    come first; everything after them names a specific domain event.
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
