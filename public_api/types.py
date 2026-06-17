import datetime
import enum

from django.http import HttpRequest

import strawberry

from organizations.models import Organization, OrganizationRole
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


@strawberry.enum
class OrgRole(enum.Enum):
    """Role a user can hold within an organization.

    Mirrors organizations.models.OrganizationRole. Keep in sync when new roles are added.
    """

    MEMBER = OrganizationRole.MEMBER
    ADMIN = OrganizationRole.ADMIN

    def to_model_role(self) -> str:
        """Return the matching OrganizationRole value string."""
        return self.value


@strawberry.input
class CreateInvitationInput:
    """Input for creating a pending organization invitation (reseller bundle).

    organizationId must be the acting org or a descendant of it.
    sendEmail defaults to True (Phase 3 only supports the email path).
    role defaults to MEMBER — admin invitations must be explicit.
    """

    user_email: str
    organization_id: strawberry.ID
    role: OrgRole = OrgRole.MEMBER
    send_email: bool = True


@strawberry.type
class InvitationResult:
    """Represents a created invitation in the API response."""

    id: int
    email: str
    expires_at: datetime.datetime


@strawberry.type
class CreateInvitationResult:
    """Result of creating an organization invitation.

    token and invite_url are null in the sendEmail=true path (Phase 3).
    Phase 4 will populate them for sendEmail=false.
    """

    invitation: InvitationResult
    token: str | None = None
    invite_url: str | None = None
