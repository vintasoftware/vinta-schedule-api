from common.types import RouteDict

from .views import OrganizationInvitationViewSet, OrganizationViewSet


routes: list[RouteDict] = [
    {
        "regex": r"organizations",
        "viewset": OrganizationViewSet,
        "basename": "Organizations",
    },
    {
        "regex": r"invitations",
        "viewset": OrganizationInvitationViewSet,
        "basename": "OrganizationInvitations",
    },
]
