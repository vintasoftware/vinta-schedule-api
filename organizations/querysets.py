from django.core.exceptions import ImproperlyConfigured
from django.db.models.query import QuerySet


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
