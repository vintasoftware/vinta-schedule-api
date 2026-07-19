from django.db.models import TextChoices
from django.utils.translation import gettext as _


class BillingState(TextChoices):
    """Billing lifecycle state of an organization's ``Subscription``.

    The spec's lifecycle diagram is the authority on the transitions between
    these states (see the Billing Plans and Limits spec).
    """

    FREE = ("free", _("Free"))
    ACTIVE = ("active", _("Active"))
    GRACE = ("grace", _("Grace period"))
    RESTRICTED = ("restricted", _("Restricted"))
    CANCELLED = ("cancelled", _("Cancelled"))


class BillingInterval(TextChoices):
    """Billing cadence for a ``Subscription``."""

    MONTHLY = ("monthly", _("Monthly"))
    ANNUAL = ("annual", _("Annual"))
