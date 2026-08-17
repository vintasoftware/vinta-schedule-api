from collections.abc import Sequence

from django.db.models import Manager

from vinta_orgs.managers import SingleOrganizationUnscopedManager

from organizations.querysets import (
    OrganizationInvitationQuerySet,
    OrganizationMembershipQuerySet,
)


# ``SingleOrganizationUnscopedManager``, not the package's scoped
# ``SingleOrganizationModelManager``: ``AbstractOrganizationMembership`` sets its
# ``objects`` to the unscoped one deliberately, and Django builds the reverse
# accessors (``user.memberships``, ``organization.memberships``) from
# ``_default_manager.__class__``. Inheriting the scoped manager instead would
# scope those accessors to the organization bound to the current context -- which
# is exactly nothing at the moment a membership lookup runs, because reading
# memberships is *how* an organization gets selected.
#
# Built with ``.from_queryset(...)`` rather than a hand-rolled ``get_queryset``
# override so ``_queryset_class`` actually points at
# ``OrganizationMembershipQuerySet``: the base's own methods
# (``for_current_organization``, ``filter_by_organization``, ...) are copied from
# ``SingleOrganizationQuerySet`` and delegate through ``get_queryset()``, so a
# manager whose ``_queryset_class`` disagreed with its methods would return the
# wrong class from half its calls.
_OrganizationMembershipManagerBase = SingleOrganizationUnscopedManager.from_queryset(
    OrganizationMembershipQuerySet
)


class OrganizationMembershipManager(_OrganizationMembershipManagerBase):  # type: ignore[misc,valid-type]
    """Unscoped manager for OrganizationMembership with domain-specific queries.

    ``occupying_a_seat``, ``billing_recipients`` and ``active_for_user`` are
    copied onto this manager from
    :class:`~organizations.querysets.OrganizationMembershipQuerySet` by
    ``from_queryset``, so ``OrganizationMembership.objects.active_for_user(user)``
    keeps working without a hand-written delegating wrapper per method.
    """


class OrganizationInvitationManager(Manager):
    """Manager for OrganizationInvitation with domain-specific query methods."""

    def get_queryset(self) -> OrganizationInvitationQuerySet:
        return OrganizationInvitationQuerySet(self.model, using=self._db)

    def pending(
        self,
        organization_ids: Sequence[int],
        exclude_id: int | None = None,
    ) -> OrganizationInvitationQuerySet:
        """Wraps :meth:`OrganizationInvitationQuerySet.pending`."""
        return self.get_queryset().pending(organization_ids, exclude_id=exclude_id)
