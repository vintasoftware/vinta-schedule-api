from django.db.models import TextChoices
from django.utils.translation import gettext as _


class BillingState(TextChoices):
    """Billing lifecycle state of an organization's ``Subscription``.

    The billing state machine's transition table is the authority on the
    transitions between these states.
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


class ProviderWebhookRoute(TextChoices):
    """Which inbound webhook endpoint received a ``ProviderWebhookEvent``.

    Scopes the idempotency ledger's uniqueness alongside ``provider`` and
    ``external_event_id`` — a provider's event-id numbering is not guaranteed to be
    disjoint between its payment and subscription-payment notification streams.
    """

    PAYMENT_UPDATE = ("payment_update", _("Payment update"))
    SUBSCRIPTION_PAYMENT_UPDATE = ("subscription_payment_update", _("Subscription payment update"))


class LimitedResource(TextChoices):
    """The closed set of resources a ``BillingPlan`` can put a ceiling on.

    Adding a member here is the only way a new resource enters the limits system —
    the ``unlimited`` plan seed enumerates this class dynamically (see the seed data
    migration + ``test_plan_seed_migration.py``) so a new member can never be silently
    missing a ``PlanLimit`` row on the rollback plan.
    """

    ORGANIZATION_MEMBERS = ("organization_members", _("Organization members"))
    RESOURCE_CALENDARS = ("resource_calendars", _("Resource calendars"))
    CALENDAR_GROUPS = ("calendar_groups", _("Calendar groups"))
    BUNDLE_CALENDARS = ("bundle_calendars", _("Bundle calendars"))
    AVAILABILITY_WINDOWS = ("availability_windows", _("Availability windows"))
    WEBHOOK_SUBSCRIPTIONS = ("webhook_subscriptions", _("Webhook subscriptions"))
    PUBLIC_API_SYSTEM_USERS = ("public_api_system_users", _("Public API system users"))
    EVENT_OCCURRENCES = ("event_occurrences", _("Event occurrences"))


class LimitKind(TextChoices):
    """Whether a ``LimitedResource`` is capped up front or metered and billed after
    the fact."""

    PREPAID = ("prepaid", _("Prepaid"))
    POSTPAID = ("postpaid", _("Postpaid"))


class LimitRemedy(TextChoices):
    """What the caller can do about an over-limit rejection.

    Rendered verbatim as the ``remedy`` key of the shared over-limit error body
    (see ``OverLimitError``), so the client can route the user to the right screen
    instead of parsing a human-readable message.
    """

    PURCHASE_ADD_ON = ("purchase_add_on", _("Purchase additional capacity"))
    UPGRADE_PLAN = ("upgrade_plan", _("Upgrade to a plan with a higher limit"))
    ADD_PAYMENT_METHOD = ("add_payment_method", _("Add a payment method"))
    RESOLVE_BILLING = ("resolve_billing", _("Resolve an outstanding billing issue"))


class Entitlement(TextChoices):
    """The closed set of boolean feature gates a ``BillingPlan`` can grant."""

    EXTERNAL_CALENDAR_GOOGLE = ("external_calendar_google", _("Google Calendar sync"))
    EXTERNAL_CALENDAR_MICROSOFT = ("external_calendar_microsoft", _("Microsoft Calendar sync"))
    PARTNER_API = ("partner_api", _("Partner / public API access"))
    WHITE_LABEL_BRANDING = ("white_label_branding", _("White-label branding"))
    ADVANCED_SCHEDULING = ("advanced_scheduling", _("Advanced scheduling"))


class LimitWarningLevel(TextChoices):
    """How close usage is to the effective limit, as reported by
    ``UsageWarningService``.

    Two distinct notifications, each debounced independently (see
    ``LimitWarningNotification``'s unique constraint) so an organization gets
    exactly one "you're close" and, separately, exactly one "you're at your
    limit" per resource per billing cycle -- never a rising flood of duplicate
    warnings as ``check_approaching_limits`` re-checks on every beat tick.
    """

    APPROACHING = ("approaching", _("Approaching the limit"))
    REACHED = ("reached", _("At or over the limit"))
