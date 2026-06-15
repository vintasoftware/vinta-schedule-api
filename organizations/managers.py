from django.db.models import Manager

from organizations.querysets import BaseOrganizationModelQuerySet, OrganizationMembershipQuerySet


class OrganizationMembershipManager(Manager):
    """Manager for OrganizationMembership with domain-specific query methods."""

    def get_queryset(self) -> OrganizationMembershipQuerySet:
        return OrganizationMembershipQuerySet(self.model, using=self._db)

    def active_for_user(self, user) -> OrganizationMembershipQuerySet:
        """Return all active memberships for *user*, ordered by creation date.

        Wraps :meth:`OrganizationMembershipQuerySet.active_for_user` so callers
        can write ``OrganizationMembership.objects.active_for_user(user)`` without
        first obtaining a queryset themselves.
        """
        return self.get_queryset().active_for_user(user)


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
