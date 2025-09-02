from typing import (
    Any,
    TypeVar,
)

from django.db import models

from strawberry import Info


Model = TypeVar("Model", bound=models.Model)


class BaseOrganizationStrawberryField[Model]:
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
        # request.public_api_organization is set by core.public_api.middlewares.PublicApiSystemUserMiddleware
        organization_id = info.context.request.public_api_organization.id
        return queryset.filter(organization_id=organization_id)
