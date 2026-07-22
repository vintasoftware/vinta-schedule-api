"""Capability gate for the reseller bundle."""

from graphql import GraphQLError
from rest_framework.exceptions import PermissionDenied

from organizations.models import Organization


def assert_org_can_invite(acting_org: Organization) -> None:
    """
    Raise PermissionDenied unless the acting org can invite/create other orgs.

    Every reseller-bundle operation checks both this and the token's OrganizationResourceAccess
    scope. The DB flag is the operator's switch; the scope is the reseller's least-privilege control
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


def is_target_in_subtree(acting_org: Organization, target_org: Organization) -> bool:
    """Return whether ``target_org`` is the acting org or a descendant of it.

    Walks the target_org's parent chain upward looking for acting_org, with a
    cycle check (visited set) against bad data. Transport-neutral so both the
    GraphQL reseller mutations (via ``assert_target_in_subtree``, which raises)
    and the REST billing permission (``IsBillingOwnerOrAdmin``, which only needs
    the boolean) can share one walk without the REST layer importing a GraphQL
    error type.
    """
    current: Organization | None = target_org
    visited: set[int] = set()

    while current is not None:
        if current.id in visited:
            # Cycle detected — bail out to prevent infinite loop.
            break
        visited.add(current.id)

        if current.id == acting_org.id:
            return True  # target_org is the acting org or a descendant.

        current = current.parent  # type: ignore[assignment]

    return False


def assert_target_in_subtree(acting_org: Organization, target_org: Organization) -> None:
    """
    Raise GraphQLError unless target_org is the acting org or a descendant of it.

    Thin raising wrapper over ``is_target_in_subtree`` (the shared walk).

    Reused by createSystemUserToken and createSystemUser, which need the same subtree
    membership check.

    Args:
        acting_org: The organization that is performing the action (the reseller).
        target_org: The organization that should be the acting org or a descendant.

    Raises:
        GraphQLError: if target_org is not in the acting_org's subtree.
    """
    if not is_target_in_subtree(acting_org, target_org):
        raise GraphQLError(
            "The target organization is not within your organization's subtree. "
            "You may only manage organizations within your own hierarchy."
        )
