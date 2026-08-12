from collections.abc import Sequence

from django.db.models import Manager

from organizations.managers import SingleOrganizationUnscopedManager

from tenancy.querysets import (
    BaseOrganizationModelQuerySet,
    OrganizationInvitationQuerySet,
    OrganizationMembershipQuerySet,
)


class OrganizationMembershipManager(
    SingleOrganizationUnscopedManager.from_queryset(  # type: ignore[misc]
        OrganizationMembershipQuerySet
    )
):
    """Manager for OrganizationMembership with domain-specific query methods.

    Built from ``SingleOrganizationUnscopedManager.from_queryset(
    OrganizationMembershipQuerySet)``, **not** ``models.Manager`` and **not**
    the scoped ``SingleOrganizationModelManager``. Two reasons, and both are
    load-bearing:

    * **Unscoped.** A membership is how an organization gets *selected*, so
      scoping the membership table to the selected organization is circular.
      ``AbstractOrganizationMembership`` sets its own ``objects`` to
      ``SingleOrganizationUnscopedManager`` for exactly that reason; replacing
      it with a scoped manager would empty ``user.memberships`` (listing the
      organizations a user belongs to), first-membership provisioning at
      signup, and every invitation-time "is this user already a member" check
      -- all of which run before anything has been selected. Django builds the
      reverse accessors from ``_default_manager.__class__``, so the mistake
      would propagate to ``user.memberships`` and ``organization.memberships``
      too.
    * **Still organization-aware.** Being the unscoped manager does not mean
      losing the scoping *methods*: ``filter_by_organization(org)`` and
      ``for_current_organization()`` come along, so a caller that does want one
      organization says so explicitly.

    Routing through ``from_queryset`` (rather than inheriting the plain
    ``SingleOrganizationUnscopedManager`` and overriding ``get_queryset``) keeps
    ``_queryset_class`` pointed at ``OrganizationMembershipQuerySet`` itself --
    the class the base manager's generated ``get_queryset`` builds
    (``self._queryset_class(model=self.model, using=self._db,
    hints=self._hints)``) -- instead of leaving it on the package's
    ``SingleOrganizationQuerySet`` while a hand-written override silently
    returned the right subclass without ``hints``. Django reads
    ``_queryset_class`` in more than one place (e.g. ``Manager._clone()``),
    and dropping ``hints`` matters for db-routing scenarios ``get_queryset()``
    otherwise threads through.
    """

    def occupying_a_seat(self, organization_ids: Sequence[int]) -> OrganizationMembershipQuerySet:
        """Wraps :meth:`OrganizationMembershipQuerySet.occupying_a_seat`."""
        return self.get_queryset().occupying_a_seat(organization_ids)

    def billing_recipients(self, organization_id: int) -> OrganizationMembershipQuerySet:
        """Wraps :meth:`OrganizationMembershipQuerySet.billing_recipients`."""
        return self.get_queryset().billing_recipients(organization_id)

    def active_for_user(self, user) -> OrganizationMembershipQuerySet:
        """Return all active memberships for *user*, ordered by creation date.

        Wraps :meth:`OrganizationMembershipQuerySet.active_for_user` so callers
        can write ``OrganizationMembership.objects.active_for_user(user)`` without
        first obtaining a queryset themselves.
        """
        return self.get_queryset().active_for_user(user)


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


class BaseOrganizationModelManager(Manager):
    """
    Base manager for organization models that need to handle calendar-related queries.
    This manager can be extended by other organization models.
    """

    def get_queryset(self):
        return BaseOrganizationModelQuerySet(self.model, using=self._db)

    def filter_by_organization(self, organization_id: int):
        """
        Filters the queryset by the specified organization ID.
        :param organization_id: ID of the organization to filter by.
        :return: Filtered queryset.
        """
        return self.get_queryset().filter(organization_id=organization_id)

    def exclude_by_organization(self, organization_id: int):
        """
        Excludes the queryset by the specified organization ID.
        :param organization_id: ID of the organization to exclude.
        :return: Filtered queryset excluding the specified organization.
        """
        return self.get_queryset().exclude(organization_id=organization_id)

    def get(self, *args, **kwargs):
        """
        Override the get method to ensure it filters by organization.
        """
        return self.get_queryset().get(*args, **kwargs)

    def count(self):
        """
        Override the count method to ensure it filters by organization.
        """
        return self.get_queryset().count()

    def create(self, **kwargs):
        """
        Override the create method to ensure it filters by organization.
        """
        if "organization_id" not in kwargs and "organization" not in kwargs:
            raise ValueError("`organization` is required to create an instance.")
        return super().create(**kwargs)
