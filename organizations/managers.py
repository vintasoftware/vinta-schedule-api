from django.db.models import Manager

from organizations.querysets import BaseOrganizationModelQuerySet


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
