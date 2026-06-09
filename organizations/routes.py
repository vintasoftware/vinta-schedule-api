from common.types import RouteDict

from .views import (
    OrganizationInvitationViewSet,
    OrganizationMembershipViewSet,
    OrganizationViewSet,
    ServiceAccountViewSet,
)


routes: list[RouteDict] = [
    {
        "regex": r"organizations",
        "viewset": OrganizationViewSet,
        "basename": "Organizations",
    },
    {
        "regex": r"service-accounts",
        "viewset": ServiceAccountViewSet,
        "basename": "ServiceAccounts",
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
