from common.types import RouteDict

from .views import OrganizationViewSet


routes: list[RouteDict] = [
    {
        "regex": r"organizations",
        "viewset": OrganizationViewSet,
        "basename": "Organizations",
    },
]
