from typing import Any

from django.db import models

from strawberry import Info


class BaseOrganizationStrawberryField[Model: models.Model]:
    @classmethod
    def get_queryset(
        cls,
        queryset: models.QuerySet[Model],
        info: Info,
        **kwargs: Any,
    ) -> models.QuerySet[Model]:
        """
        Filters the queryset based on the organization ID from the request context.
        """
        # request.public_api_organization is set by public_api.middlewares.PublicApiSystemUserMiddleware
        organization_id = info.context.request.public_api_organization.id
        return queryset.filter(organization_id=organization_id)
