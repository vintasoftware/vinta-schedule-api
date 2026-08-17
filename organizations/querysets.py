from __future__ import annotations

from collections.abc import Sequence

from django.db.models.query import QuerySet
from django.utils import timezone

from vinta_orgs.querysets import (
    OrganizationMembershipQuerySet as _PackageOrganizationMembershipQuerySet,
)

from organizations.permission_catalog import MANAGE_BILLING


class OrganizationMembershipQuerySet(_PackageOrganizationMembershipQuerySet):
    """QuerySet for OrganizationMembership with domain-specific filtering methods.

    Built on the package's own ``OrganizationMembershipQuerySet`` -- not shadowed
    by a same-named class built on a plainer base -- so ``0.3.0``'s membership
    lookups (``active()``, ``active_for_user()``, ``holding_permission()``) stay
    available here rather than being silently replaced by a class of the same
    name that does not implement them. It does **not** scope implicitly -- that
    is a property of the *manager* (see
    ``organizations.managers.OrganizationMembershipManager``), and a membership
    is the row you read to decide which organization to select, so scoping it
    to the selected organization would be circular.
    """

    def occupying_a_seat(self, organization_ids: Sequence[int]) -> OrganizationMembershipQuerySet:
        """Memberships in ``organization_ids`` that consume a licensed seat.

        Only active memberships count: deactivating a member is how a seat is
        freed, so counting inactive rows would make removal fail to free
        capacity. Lives here rather than in the billing service because
        "``is_active=False`` is this model's soft delete" is a fact about
        ``OrganizationMembership``, not about billing.

        Narrows through the inherited ``active()`` rather than a hand-written
        ``is_active=True``, for the same reason ``billing_recipients`` does:
        one spelling of "this membership still grants something", and it is the
        same one ``OrganizationModelBackend._get_membership`` applies.
        """
        return self.filter(organization_id__in=organization_ids).active()

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
        that can drift. It replaces the flat two-column disjunction it was
        written from,
        and is now the only representation: the Phase 3 backfill
        (``organizations/migrations/0029_...``) put every pre-existing membership
        in the matching group, and every membership written since goes through
        ``organizations.services.assign_membership_groups``.

        Built on ``holding_permission(...)`` -- the package's own union of a
        membership's direct ``permissions`` grant with the permissions its
        ``groups`` carry -- rather than a hand-written ``groups__permissions``
        filter, so this cannot drift from what
        ``vinta_orgs.authorization.has_organization_permission`` (Phase 4) and
        the last-administrator count both read. ``holding_permission`` already
        matches ``content_type__app_label`` alongside the codename and already
        calls ``distinct()`` -- see its docstring for why both are necessary.

        ``active()`` narrows to ``is_active=True`` memberships first, for the
        same reason ``occupying_a_seat`` does: a deactivated member receives no
        dunning notifications.
        """
        return (
            self.filter(organization_id=organization_id).active().holding_permission(MANAGE_BILLING)
        )

    def other_members_holding(
        self, *, organization_id: int, excluding_user_id: int, permission: str
    ) -> OrganizationMembershipQuerySet:
        """Active memberships in ``organization_id``, other than
        ``excluding_user_id``'s, that hold ``permission``.

        The "would this write leave nobody able to X" question, asked once. Both
        last-administrator guards in ``organizations/views.py`` (``deactivate`` and
        ``assign_groups``) need exactly this count, and each used to spell the three
        clauses inline -- which is both the inline-queryset rule in ``AGENTS.md`` and,
        more to the point, two chances for the guards to stop meaning the same thing.
        The pair *must* agree: a widening in one and not the other is a lockout in one
        endpoint and a refusal in the other, for the same organization.

        A membership is keyed by ``(user, organization)``, so excluding the target by
        ``user_id`` inside the already organization-scoped filter names exactly one row.

        ``holding_permission`` is the package's union of a membership's direct
        ``permissions`` grant with the permissions its ``groups`` carry, so a direct
        per-membership grant counts as a remaining holder just as much as
        ``organization_admin`` does.
        """
        return (
            self.filter(organization_id=organization_id)
            .active()
            .exclude(user_id=excluding_user_id)
            .holding_permission(permission)
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
