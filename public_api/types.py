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


@strawberry.input
class CreateSystemUserTokenInput:
    """Input for minting a delegated Public API token (reseller bundle).

    organization_id must be the acting org or a descendant of it.
    resources must be a non-empty list of valid PublicAPIResources values.
    ORGANIZATION may be included to delegate the invite-orgs capability for
    tokens the reseller mints — the minted token still cannot set the DB flag.
    """

    organization_id: strawberry.ID
    integration_name: str
    resources: list[str]


@strawberry.type
class CreateSystemUserTokenResult:
    """Result of minting a delegated Public API token.

    system_user_id and token are returned once; the plaintext token is never persisted.
    """

    system_user_id: strawberry.ID
    token: str


@strawberry.input
class UpdateBrandingInput:
    """Input for updating reseller branding.

    Updates branding on the acting org (must be a reseller). Always upserts
    (creates if missing, updates if exists). Cannot target another org's tree.
    """

    app_name: str
    logo_url: str = ""
    primary_color: str = ""
    secondary_color: str = ""
    support_email: str = ""
    return_url_allowlist: list[str] | None = None


@strawberry.type
class BrandingResult:
    """Represents resolved branding in the API response.

    Never includes secrets like support_email or allowlist; those are for internal
    use only (email rendering, OAuth return-URL validation).
    """

    id: int
    app_name: str
    logo_url: str
    primary_color: str
    secondary_color: str


@strawberry.type
class UpdateBrandingResult:
    """Result of updating branding."""

    branding: BrandingResult | None


@strawberry.type
class PublicBrandingResult:
    """Represents public, secret-free branding for unauthenticated access.

    Used by brandingForTenant query for frontend interstitials.
    Excludes the branding row id, support_email, and return_url_allowlist.
    """

    app_name: str
    logo_url: str
    primary_color: str
    secondary_color: str


@strawberry.type
class ChildOrganizationMetrics:
    """Point-in-time aggregate counts for a child organization.

    Returned by the childOrganizations analytics query (resource: CHILD_ORG_ANALYTICS).
    Counts are computed via ORM subqueries to avoid join fan-out double-counting.
    """

    id: int
    name: str
    created_at: datetime.datetime
    membership_count: int
    calendar_count: int
    event_count: int
    calendar_group_count: int
