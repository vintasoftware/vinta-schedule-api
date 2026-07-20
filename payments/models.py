from typing import TYPE_CHECKING, ClassVar

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, UniqueConstraint

from common.models import BaseModel
from payments.billing_constants import (
    BillingInterval,
    BillingState,
    Entitlement,
    LimitedResource,
    LimitKind,
    ProviderWebhookRoute,
)
from payments.constants import (
    PaymentProviders,
    PaymentStatuses,
    RefundStatuses,
    SubscriptionStatuses,
)
from payments.managers import MeteredOccurrenceManager, ProviderWebhookEventManager


if TYPE_CHECKING:
    from django_stubs_ext.db.models.manager import RelatedManager


class BillingAddress(BaseModel):
    street_name = models.TextField()
    street_number = models.TextField()
    neighborhood = models.TextField(blank=True)
    address_line_2 = models.TextField(blank=True)
    city = models.CharField(max_length=255)
    state = models.CharField(max_length=255)
    country = models.CharField(max_length=255)
    zip_code = models.CharField(max_length=10)

    billing_profile: "BillingProfile"

    def __str__(self):
        return f"{self.id} {self.organization} - {self.city} - {self.state} - {self.country} - {self.zip_code}"

    @property
    def organization(self):
        return getattr(self, "billing_profile", None) and self.billing_profile.organization


class BillingPlan(BaseModel):
    """Catalog plan that a ``Subscription`` is sold against.

    Carries its ``PlanLimit`` / ``PlanEntitlement`` rows (the plan catalog proper).
    There is no feature flag for the limits/entitlements rollout: the ``unlimited``
    plan — every ``PlanLimit.limit_value`` NULL, every ``PlanEntitlement`` enabled —
    *is* the kill switch. Catalog edits here never propagate to an already-sold
    subscription; see ``SubscriptionPlanLimit`` (a later phase) for the
    per-subscription copy.
    """

    slug = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True, db_index=True)
    is_default_for_new_organizations = models.BooleanField(default=False)
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2)
    annual_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3)
    grace_period_days = models.PositiveIntegerField(null=True, blank=True)

    subscriptions: "RelatedManager[Subscription]"
    limits: "RelatedManager[PlanLimit]"
    entitlements: "RelatedManager[PlanEntitlement]"

    #: Per-instance opt-out of the limit-coverage check in ``clean`` — set by
    #: ``BillingPlanAdmin``'s form, which validates coverage on the inline formset
    #: instead (the only place that can see the rows the save is about to write).
    skip_limit_coverage_validation: bool = False

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["is_default_for_new_organizations"],
                condition=Q(is_default_for_new_organizations=True),
                name="uniq_default_billing_plan",
            )
        ]

    def __str__(self):
        return self.name

    def get_missing_limited_resource_keys(self) -> list[str]:
        """``LimitedResource`` members this plan carries no ``PlanLimit`` row for.

        A plan is *complete* when this is empty. Completeness is an invariant, not
        a preference: an absent ``PlanLimit`` row (or a stale
        ``SubscriptionPlanLimit`` left over from a previous plan with
        ``limit_value=None``) reads as **unlimited** in ``EntitlementService``, so
        an incomplete plan silently grants an infinite ceiling on the resource it
        omits. "Not included" is expressed with ``limit_value=0``, never omission.

        An unsaved plan has no rows to read (the related manager raises on an
        instance with no pk), so it is reported as missing everything — which is
        what ``clean`` should say about it.
        """
        expected = set(LimitedResource.values)
        if self.pk is None:
            return sorted(expected)
        covered = set(self.limits.values_list("resource_key", flat=True))
        return sorted(expected - covered)

    def clean(self) -> None:
        """Reject an incomplete plan at authoring time rather than at downgrade time.

        ``BillingPlanAdmin`` skips this one check (see
        ``BillingPlanAdmin.form``) because the parent form is validated *before*
        its ``PlanLimit`` inline formset is saved — the rows that would make the
        plan complete are still pending, so this would reject the very edit that
        fixes it, with no way out. The admin runs the equivalent check on the
        inline formset instead, against the rows the save is about to produce.
        """
        super().clean()
        if self.skip_limit_coverage_validation:
            return
        missing = self.get_missing_limited_resource_keys()
        if missing:
            raise ValidationError(
                {
                    "__all__": (
                        f"This plan has no PlanLimit row for {missing}. Every plan must "
                        "carry a row for every limited resource — 'not included' is "
                        "limit_value=0, never omission, because an omitted row reads as "
                        "unlimited."
                    )
                }
            )


class PlanLimit(BaseModel):
    """A single resource ceiling on a ``BillingPlan``.

    ``limit_value=NULL`` means no ceiling (unlimited) — never treat NULL as zero.
    ``kind`` mirrors ``LimitedResource``'s own prepaid/postpaid split so an
    effective-limit resolution does not have to cross-reference the choices class.
    """

    plan = models.ForeignKey(BillingPlan, on_delete=models.CASCADE, related_name="limits")
    resource_key = models.CharField(max_length=100, choices=LimitedResource)
    limit_value = models.PositiveIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=20, choices=LimitKind)
    overage_unit_price = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["plan", "resource_key"],
                name="uniq_plan_limit_resource",
            )
        ]

    def __str__(self):
        return f"{self.plan} - {self.resource_key} - {self.limit_value}"


class PlanEntitlement(BaseModel):
    """A single boolean feature gate on a ``BillingPlan``."""

    plan = models.ForeignKey(BillingPlan, on_delete=models.CASCADE, related_name="entitlements")
    entitlement_key = models.CharField(max_length=100, choices=Entitlement)
    is_enabled = models.BooleanField(default=False)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["plan", "entitlement_key"],
                name="uniq_plan_entitlement_key",
            )
        ]

    def __str__(self):
        return f"{self.plan} - {self.entitlement_key} - {self.is_enabled}"


class BillingProfile(BaseModel):
    organization = models.OneToOneField(
        "organizations.Organization",
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="billing_profile",
    )
    # Payer identity sent to the payment gateway. Distinct from the future
    # `OrganizationMembership.is_billing_owner` (Phase 9), which is about who may
    # *manage* billing — these fields are about what the gateway needs to charge
    # the organization (e.g. MercadoPago rejects a payer with no email).
    contact_first_name = models.CharField(max_length=255)
    contact_last_name = models.CharField(max_length=255, blank=True)
    contact_email = models.EmailField()
    contact_phone = models.CharField(max_length=50, blank=True)
    document_type = models.CharField(max_length=50)
    document_number = models.CharField(max_length=50)
    billing_address = models.OneToOneField(
        BillingAddress, on_delete=models.CASCADE, related_name="billing_profile"
    )

    def __str__(self):
        return f"{self.pk} {self.organization} - {self.document_type} - {self.document_number}"


class Subscription(BaseModel):
    """An organization's subscription to a ``BillingPlan``.

    Two status concepts coexist here and share member names (``active``,
    ``cancelled``, ``pending``) — do not conflate them:

    - ``status`` (``SubscriptionStatuses``) mirrors the provider-reported state of
      the subscription, fed by ``SubscriptionStatusUpdate`` rows as the gateway
      reports them (e.g. MercadoPago's ``authorized`` / ``paused`` / ``cancelled``).
    - ``billing_state`` (``BillingState``) is this app's internal billing
      lifecycle (free / active / grace / restricted / cancelled) used to gate
      access. It is derived from, but not identical to, ``status``.
    """

    organization = models.OneToOneField(
        "organizations.Organization", on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.ForeignKey(BillingPlan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(
        max_length=50, choices=SubscriptionStatuses, default=SubscriptionStatuses.PENDING_SEND
    )
    billing_state = models.CharField(
        max_length=20, choices=BillingState, default=BillingState.FREE, db_index=True
    )
    billing_interval = models.CharField(
        max_length=10, choices=BillingInterval, default=BillingInterval.MONTHLY
    )
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField(db_index=True)
    grace_period_ends_at = models.DateTimeField(null=True, blank=True, db_index=True)
    external_id = models.CharField(max_length=255, blank=True, db_index=True)
    plan_external_id = models.CharField(max_length=255, blank=True)
    payment_provider = models.CharField(max_length=50, choices=PaymentProviders)

    limits: "RelatedManager[SubscriptionPlanLimit]"
    entitlements: "RelatedManager[SubscriptionEntitlement]"
    add_ons: "RelatedManager[SubscriptionAddOn]"

    def __str__(self):
        return (
            f"{self.id} - {self.status} - {self.current_period_start} - {self.current_period_end}"
        )


class SubscriptionPlanLimit(BaseModel):
    """Per-subscription copy of a ``PlanLimit`` row — the support lever.

    Copied from the catalog ``PlanLimit`` on subscription creation and re-copied on
    plan change (``SubscriptionService.change_plan``). Catalog edits to ``PlanLimit``
    never propagate here — an organization keeps what it was sold, and a catalog typo
    cannot silently lower limits for every subscriber at once.

    ``is_overridden=True`` marks a row an admin edited by hand in Django admin (see
    ``payments/admin.py``'s ``SubscriptionPlanLimitInline``) — this is the support
    lever for a stuck organization, and it is why there is no support-facing
    enforcement bypass elsewhere. A plan change re-copies every non-overridden row
    from the new plan's ``PlanLimit`` set and leaves ``is_overridden=True`` rows
    untouched.
    """

    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="limits")
    resource_key = models.CharField(max_length=100, choices=LimitedResource)
    limit_value = models.PositiveIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=20, choices=LimitKind)
    overage_unit_price = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    is_overridden = models.BooleanField(default=False)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["subscription", "resource_key"],
                name="uniq_sub_limit_resource",
            )
        ]

    def __str__(self):
        return f"{self.subscription} - {self.resource_key} - {self.limit_value}"


class SubscriptionEntitlement(BaseModel):
    """Per-subscription copy of a ``PlanEntitlement`` row.

    Mirrors ``SubscriptionPlanLimit``'s override semantics: catalog edits do not
    propagate, and ``is_overridden=True`` rows survive a plan change untouched.
    """

    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="entitlements"
    )
    entitlement_key = models.CharField(max_length=100, choices=Entitlement)
    is_enabled = models.BooleanField(default=False)
    is_overridden = models.BooleanField(default=False)

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["subscription", "entitlement_key"],
                name="uniq_sub_entitlement_key",
            )
        ]

    def __str__(self):
        return f"{self.subscription} - {self.entitlement_key} - {self.is_enabled}"


class SubscriptionAddOn(BaseModel):
    """Extra capacity bought on top of a ``Subscription``'s plan limits.

    An active add-on's ``quantity`` is added to the matching
    ``SubscriptionPlanLimit.limit_value`` when resolving the effective ceiling
    (``EntitlementService.get_effective_limit``). An add-on on a resource whose
    limit is NULL (unlimited) changes nothing — unlimited plus anything is still
    unlimited.

    ``purchase_idempotency_key`` is unique so a retried purchase (a double-clicked
    button, a Celery task re-delivered under ``CELERY_TASK_ACKS_LATE``) neither
    grants capacity twice nor charges twice. The purchase flow that populates it
    lands in a later phase; the constraint exists from the start so no code path
    can be written against a non-idempotent shape.
    """

    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="add_ons")
    resource_key = models.CharField(max_length=100, choices=LimitedResource)
    quantity = models.PositiveIntegerField()
    is_recurring = models.BooleanField()
    is_active = models.BooleanField(default=True, db_index=True)
    external_id = models.CharField(max_length=255, blank=True)
    purchase_idempotency_key = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return f"{self.subscription} - {self.resource_key} - +{self.quantity}"


class Payment(BaseModel):
    value = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=50)
    payment_provider = models.CharField(max_length=50, choices=PaymentProviders)
    external_id = models.CharField(max_length=255)
    status = models.CharField(max_length=50, choices=PaymentStatuses)
    original_status = models.CharField(max_length=50)
    billing_profile = models.ForeignKey(
        BillingProfile, on_delete=models.CASCADE, related_name="payments"
    )
    payment_method = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="payments", null=True, blank=True
    )

    status_updates: "RelatedManager[PaymentStatusUpdate]"

    def __str__(self):
        return f"{self.id} {self.organization} - {self.value} - {self.payment_provider} - {self.status} - {self.created.isoformat()}"

    @property
    def organization(self):
        return getattr(self, "billing_profile", None) and self.billing_profile.organization


class Refund(BaseModel):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="refunds")
    value = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=50)
    external_id = models.CharField(max_length=255)
    status = models.CharField(
        max_length=50, choices=RefundStatuses, default=RefundStatuses.PENDING_SEND
    )

    def __str__(self):
        return f"{self.id} {self.payment} - {self.value} - {self.currency} - {self.created.isoformat()}"


class PaymentStatusUpdate(BaseModel):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="status_updates")
    status = models.CharField(max_length=50, choices=PaymentStatuses)
    description = models.TextField(blank=True)
    external_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.id} {self.payment} - {self.status} - {self.created.isoformat()}"


class SubscriptionStatusUpdate(BaseModel):
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="status_updates"
    )
    status = models.CharField(max_length=50, choices=SubscriptionStatuses)
    description = models.TextField(blank=True)
    external_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.id} {self.subscription} - {self.status} - {self.created.isoformat()}"


class RefundStatusUpdate(BaseModel):
    refund = models.ForeignKey(Refund, on_delete=models.CASCADE, related_name="status_updates")
    status = models.CharField(max_length=50, choices=RefundStatuses)
    description = models.TextField(blank=True)
    external_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.id} {self.refund} - {self.status} - {self.created.isoformat()}"


class ProviderWebhookEvent(BaseModel):
    """Idempotency ledger for inbound payment-provider webhook notifications.

    Not tenant-scoped: a webhook notification arrives before we know which
    organization it resolves to (see the billing plans and limits plan's Data Model
    Changes — cross-organization billing reads are the reason these models stay
    plain-FK rather than ``OrganizationModel``). ``(provider, route,
    external_event_id)`` uniquely identifies one delivery attempt at the provider;
    ``processed_at`` is set only once the corresponding domain update
    (payment/subscription status) has actually been applied, so a row that exists
    with ``processed_at=None`` means a previous delivery was recorded but crashed
    before finishing — the next delivery for the same event is allowed to retry
    rather than being silently dropped.
    """

    provider = models.CharField(max_length=50, choices=PaymentProviders)
    route = models.CharField(max_length=50, choices=ProviderWebhookRoute)
    external_event_id = models.CharField(max_length=255)
    payload = models.JSONField(default=dict, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects: ClassVar[ProviderWebhookEventManager] = ProviderWebhookEventManager()

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["provider", "route", "external_event_id"],
                name="uniq_provider_webhook_event",
            )
        ]

    def __str__(self):
        return f"{self.provider} - {self.route} - {self.external_event_id}"


class MeteredOccurrence(BaseModel):
    """One event occurrence, recorded as billable exactly once, ever.

    Occurrences of a recurring series are *computed* in Postgres, never stored
    (``calculate_recurring_events`` and friends), so there is no row to bill
    against. This table is that row — written by ``MeteringService`` from a
    Celery sweep of elapsed time.

    **The unique constraint is the correctness mechanism, not the code path.**
    ``(organization, event_id, occurrence_start)`` plus
    ``bulk_create(..., ignore_conflicts=True)`` is what makes re-running a window,
    or running two windows that overlap, harmless. The sweep window deliberately
    overlaps the previous one so that a missed run self-heals on the next pass;
    that is only safe because a re-insert is a no-op at the database level rather
    than something application code has to remember to check. Do not replace it
    with an application-level "have I already seen this?" lookup — that lookup
    races itself, and the failure is a silent wrong number on an invoice rather
    than an exception.

    ``event_id`` is a soft reference (``BigIntegerField``, not a ``ForeignKey``) on
    purpose: deleting an event must not delete the record that its occurrences were
    billed. An occurrence is billed at most once *ever*, and that fact has to
    outlive the event.

    What the two identity columns actually hold is narrower than their names
    suggest, and the constraint only works because of it. ``event_id`` is the
    **series root** (the original master, following ``bulk_modification_parent``
    back through any splits) and ``occurrence_start`` is the **recurrence slot** the
    occurrence occupies — not the row that currently represents it, nor the time it
    was ultimately moved to. Editing an occurrence writes a new ``CalendarEvent``,
    and splitting a series moves later occurrences onto a new one; identifying by
    those rows would make an already-billed occurrence look new. See
    ``MeteringService.expand_occurrence_identities``.

    ``is_within_allowance`` and ``unit_price`` are stamped **at meter time** against
    the allowance and overage price in force at that moment, so a later plan change
    or limit override cannot retroactively reprice usage that already happened.

    Not an ``OrganizationModel``: billing legitimately reads across organizations
    (a reseller root's cycle close sums its whole subtree), and the tenant-safe
    queryset layer would force an ``original_manager`` escape at nearly every call
    site. The ``organization`` FK is still present and every read goes through
    ``MeteredOccurrenceQuerySet.for_organizations``.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="metered_occurrences",
    )
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="metered_occurrences"
    )
    event_id = models.BigIntegerField()
    occurrence_start = models.DateTimeField()
    billing_period_start = models.DateTimeField(db_index=True)
    is_within_allowance = models.BooleanField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=4)

    objects: ClassVar[MeteredOccurrenceManager] = MeteredOccurrenceManager()

    class Meta(BaseModel.Meta):
        constraints: ClassVar = [
            UniqueConstraint(
                fields=["organization", "event_id", "occurrence_start"],
                name="uniq_metered_occurrence",
            )
        ]
        indexes: ClassVar = [
            models.Index(
                fields=["subscription", "billing_period_start"],
                name="metered_occ_sub_period_idx",
            )
        ]

    def __str__(self):
        return f"{self.organization_id}/{self.event_id} @ {self.occurrence_start.isoformat()}"
