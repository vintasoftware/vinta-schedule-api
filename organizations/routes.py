from django.urls import path

from common.types import RouteDict

from .views import (
    OrganizationBrandingView,
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

# Non-viewset routes (APIViews) — URL patterns to register with Django URL conf
extra_patterns = [
    path("branding/", OrganizationBrandingView.as_view(), name="branding"),
]
