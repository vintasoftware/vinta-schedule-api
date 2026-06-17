"""Capability gate for the reseller bundle."""

from rest_framework.exceptions import PermissionDenied

from organizations.models import Organization


def assert_org_can_invite(acting_org: Organization) -> None:
    """
    Gate: raise PermissionDenied unless the acting org can invite/create other orgs.

    Every reseller-bundle operation checks both this gate and the token's OrganizationResourceAccess
    scope. The DB flag is the operator's switch; the scope is the reseller's lease-privilege control
    over its own tokens.

    Args:
        acting_org: The Organization instance performing the action.

    Raises:
        PermissionDenied: if acting_org.can_invite_organizations is False.
    """
    if not acting_org.can_invite_organizations:
        raise PermissionDenied(
            "This organization does not have permission to invite or create other organizations."
        )
