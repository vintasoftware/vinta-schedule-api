from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from django.db.models.query import QuerySet
from django.utils import timezone

from vinta_orgs.querysets import SingleOrganizationQuerySet


if TYPE_CHECKING:
    from users.models import User


class OrganizationMembershipQuerySet(SingleOrganizationQuerySet):
    """QuerySet for OrganizationMembership with domain-specific filtering methods.

    Built on the package's ``SingleOrganizationQuerySet`` rather than a plain
    ``QuerySet`` so that ``filter_by_organization(...)`` /
    ``for_current_organization()`` chain off it the same way they do off every
    other organization-scoped model. It does **not** scope implicitly -- that
    is a property of the *manager* (see
    ``organizations.managers.OrganizationMembershipManager``), and a membership
    is the row you read to decide which organization to select, so scoping it
    to the selected organization would be circular.
    """

    def occupying_a_seat(self, organization_ids: Sequence[int]) -> OrganizationMembershipQuerySet:
        """Memberships in ``organization_ids`` that consume a licensed seat.

        Only ``is_active=True`` memberships count: deactivating a member is how a
        seat is freed, so counting inactive rows would make removal fail to free
        capacity. Lives here rather than in the billing service because
        "``is_active=False`` is this model's soft delete" is a fact about
        ``OrganizationMembership``, not about billing.
        """
        return self.filter(organization_id__in=organization_ids, is_active=True)

    def billing_recipients(self, organization_id: int) -> OrganizationMembershipQuerySet:
        """Active memberships eligible to receive billing/dunning notifications for
        ``organization_id``: the ones holding ``payments.manage_billing``.

        Used by ``DunningService`` (``payments/services/dunning_service.py``) and
        ``UsageWarningService`` to resolve who receives the dunning ladder's
        email/in-app notifications -- billing is organization-owned, not
        user-owned, so there is no single "the" recipient; every eligible member
        gets one.

        **Permission-shaped rather than role-shaped**, so "who may write billing"
        and "who is told about billing" derive from one source instead of two
        that can drift. It replaces ``Q(role=ADMIN) | Q(is_billing_owner=True)``,
        and is equivalent to it as long as the two representations agree: the
        Phase 3 backfill (``organizations/migrations/0029_...``) put every
        pre-existing membership in the matching group and the dual-write in
        ``organizations.services`` keeps every membership written since in step.

        ``content_type__app_label`` is matched alongside the codename even though
        ``manage_billing`` is unique across the catalog today -- both lookups sit
        in one ``filter()`` call, so they bind to the *same* permission row, and a
        future ``manage_billing`` declared in some other app cannot silently widen
        who receives dunning.

        ``distinct()`` is not optional: the two chained many-to-many joins
        (membership -> group -> permission) produce one row per *path*, so a
        membership in both ``organization_admin`` and
        ``organization_billing_owner`` -- the shape the backfill writes for an
        admin who is also flagged ``is_billing_owner`` -- would otherwise appear
        twice and be notified twice.

        Only *group*-held permissions count. ``OrganizationMembership.permissions``
        (the per-membership direct grant the package also provides) is empty
        everywhere in this codebase and nothing writes it; adding it here would
        mean a second multi-valued join under an ``OR``.
        """
        return (
            self.filter(organization_id=organization_id, is_active=True)
            .filter(
                groups__permissions__codename="manage_billing",
                groups__permissions__content_type__app_label="payments",
            )
            .distinct()
        )

    def active_for_user(self, user: User) -> OrganizationMembershipQuerySet:
        """Return all active memberships for *user*, with organization pre-fetched.

        Ordered by creation date (oldest first) so the result is deterministic
        for the org-switcher list.  ``select_related("organization")`` avoids
        an N+1 when iterating over the returned memberships.
        """
        return (
            self.filter(user=user, is_active=True)
            .select_related("organization")
            .order_by("created")
        )


class OrganizationInvitationQuerySet(QuerySet):
    """QuerySet for OrganizationInvitation with domain-specific filtering methods."""

    def pending(
        self,
        organization_ids: Sequence[int],
        exclude_id: int | None = None,
    ) -> OrganizationInvitationQuerySet:
        """Invitations in ``organization_ids`` that can still turn into a seat.

        Neither an already-accepted invitation (its seat is the membership row) nor
        an expired one (it can never be accepted) can become a seat, so both are
        excluded.

        :param exclude_id: An invitation to leave out of the result. Used by the
            accept path, which is net-zero on seat count — the invitation being
            accepted stops being pending and becomes the membership it is already
            reserving capacity for. Without it, an organization sitting exactly at
            its ceiling could never accept its own last outstanding invitation.
        """
        queryset = self.filter(
            organization_id__in=organization_ids,
            accepted_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        if exclude_id is not None:
            queryset = queryset.exclude(pk=exclude_id)
        return queryset
