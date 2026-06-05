from common.types import RouteDict

from .views import (
    OrganizationInvitationViewSet,
    OrganizationMembershipViewSet,
    OrganizationViewSet,
)


routes: list[RouteDict] = [
    {
        "regex": r"organizations",
        "viewset": OrganizationViewSet,
        "basename": "Organizations",
    },
    {
        "regex": r"organization-members",
        "viewset": OrganizationMembershipViewSet,
        "basename": "OrganizationMembers",
    },
    {
        "regex": r"invitations",
        "viewset": OrganizationInvitationViewSet,
        "basename": "OrganizationInvitations",
    },
]
