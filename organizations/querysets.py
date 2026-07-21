from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.db.models.query import QuerySet
from django.utils import timezone


if TYPE_CHECKING:
    from users.models import User


class OrganizationMembershipQuerySet(QuerySet):
    """QuerySet for OrganizationMembership with domain-specific filtering methods."""

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
        ``organization_id``: admins and billing owners (``is_billing_owner=True``)
        -- the same two roles ``IsBillingOwnerOrAdmin`` gates billing writes to.

        Used by ``DunningService`` (``payments/services/dunning_service.py``) to
        resolve who receives the dunning ladder's email/in-app notifications --
        billing is organization-owned, not user-owned (Phase 1's guiding
        decision), so there is no single "the" recipient; every eligible member
        gets one.

        ``OrganizationRole`` is imported here rather than at module level to avoid
        a cycle: ``organizations.models`` imports this module (via
        ``organizations.managers``), so this module cannot import back from
        ``organizations.models`` at import time.
        """
        from organizations.models import OrganizationRole

        return self.filter(organization_id=organization_id, is_active=True).filter(
            Q(role=OrganizationRole.ADMIN) | Q(is_billing_owner=True)
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


class BaseOrganizationModelQuerySet(QuerySet):
    """
    Base QuerySet for organization models that need to filter by organization.

    This ensures that all queries are scoped to the organization
    """

    def filter_by_organization(self, organization_id: int):
        """
        Filters the queryset by the specified organization ID.
        :param organization_id: ID of the organization to filter by.
        :return: Filtered QuerySet.
        """
        return super().filter(organization_id=organization_id)

    def exclude_by_organization(self, organization_id: int):
        """
        Excludes records belonging to the specified organization ID.
        :param organization_id: ID of the organization to exclude.
        :return: Filtered QuerySet.
        """
        return super().exclude(organization_id=organization_id)

    def _check_required_tenant_filter(self):
        required_field = "organization"
        where_str = str(self.query.where)
        if required_field not in where_str and f"{required_field}_id" not in where_str:
            raise ImproperlyConfigured(
                f"QuerySet must be filtered by `{required_field}` on model {self.model}"
            )

    def __iter__(self):
        self._check_required_tenant_filter()
        return super().__iter__()

    def count(self):
        self._check_required_tenant_filter()
        return super().count()

    def get(self, *args, **kwargs):
        if (
            "organization_id" not in kwargs
            and "organization_id" not in str(self.query.where)
            and "organization" not in kwargs
            and "organization" not in str(self.query.where)
        ):
            raise ImproperlyConfigured(
                f"`organization_id` filter is required when querying model {self.model}."
            )
        return super().get(*args, **kwargs)

    def update(self, **kwargs):
        from common.fields import TenantSafeForeignKey, TenantSafeOneToOneField

        if "organization_id" in kwargs or "organization" in kwargs:
            raise ValueError("`organization` cannot be updated.")

        tenant_safe_foreign_keys = [
            field.name
            for field in self.model._meta.get_fields(include_hidden=True, include_parents=False)
            if isinstance(field, TenantSafeForeignKey) or isinstance(field, TenantSafeOneToOneField)
        ]

        for field_name in tenant_safe_foreign_keys:
            if field_name in kwargs:
                kwargs[f"{field_name}_fk"] = kwargs.pop(field_name, None)
            elif field_name + "_id" in kwargs:
                kwargs[f"{field_name}_fk_id"] = kwargs.pop(f"{field_name}_id", None)

        return super().update(**kwargs)
