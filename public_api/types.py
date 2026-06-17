from django.http import HttpRequest

import strawberry

from organizations.models import Organization
from public_api.models import SystemUser


class PublicApiHttpRequest(HttpRequest):
    public_api_system_user: SystemUser | None
    public_api_organization: Organization | None


@strawberry.type
class OrganizationResult:
    """Represents an organization in the API response."""

    id: int
    name: str


@strawberry.input
class CreateOrganizationInput:
    """Input for creating a child organization."""

    name: str


@strawberry.type
class CreateOrganizationResult:
    """Result of creating an organization."""

    organization: OrganizationResult


@strawberry.input
class CreateUserInput:
    """Input for creating a passwordless user."""

    email: str
    first_name: str | None = None
    last_name: str | None = None


@strawberry.type
class UserResult:
    """Represents a user in the API response."""

    id: int
    email: str
    first_name: str | None = None
    last_name: str | None = None


@strawberry.type
class CreateUserResult:
    """Result of creating a user."""

    user: UserResult
