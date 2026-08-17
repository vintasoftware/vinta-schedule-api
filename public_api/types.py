import datetime

from django.http import HttpRequest

import strawberry

from organizations.models import Organization
from organizations.permission_catalog import GROUP_ORGANIZATION_MEMBER
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
class CreateInvitationInput:
    """Input for creating a pending organization invitation (reseller bundle).

    organizationId must be the acting org or a descendant of it.
    sendEmail defaults to True.
    groups defaults to ["organization_member"] — an invitation that confers any
    capability must name the group that carries it explicitly.

    Replaces the former ``role: OrgRole`` field, and the ``OrgRole`` enum went
    with it: authorization is answered from permissions, which are carried by
    groups, so a group name is the only thing left worth naming on a write.
    Accepted values are validated in
    ``organizations.permission_catalog.group_for_invitation_groups`` rather than
    by a GraphQL enum, so adding a group is not a schema-breaking change for
    clients that do not use it.
    """

    user_email: str
    organization_id: strawberry.ID
    groups: list[str] = strawberry.field(
        default_factory=lambda: [GROUP_ORGANIZATION_MEMBER],
        description=(
            "Groups the invited user's membership receives on acceptance. "
            "'organization_member' (the default) confers no capability; "
            "'organization_admin' confers every capability. "
            "'organization_billing_owner' cannot be assigned at invitation "
            "time -- assign it after acceptance."
        ),
    )
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

    token and invite_url are null when sendEmail is true (the email path).
    When sendEmail is false, they hold the raw token and invite URL.
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
    """Input for updating an organization's branding.

    Updates branding on the acting org. Always upserts (creates if missing,
    updates if exists). Cannot target another org's tree. The acting org must
    pass the shared branding write gate (parentless and entitled --
    ``organizations.permissions.evaluate_branding_write_gate``); a reseller is
    not exempt from either condition.

    ``logo_url`` is write-only despite the name (kept for symmetry with the REST
    serializer's field): it accepts the S3 key returned by
    ``createBrandingLogoUpload`` (a bare key or a full signed/public URL, either
    way normalized to a key before it is stored). Reads never echo this value
    back — ``BrandingResult.logo_url`` is always the logo delivery route's URL.

    ``slug``: optional. When supplied, it is validated with the same shared
    rules the organization REST endpoint uses (``organizations.slug_validation
    .validate_organization_slug``, plus a uniqueness check excluding the acting
    org itself) and applied to the acting organization -- so a partner-API
    caller can rename its public identifier and set branding in a single call,
    rather than needing a separate organization-update mutation that does not
    exist on this surface. Three states, matching the REST serializer's
    ``validate_slug`` on update: **omitted** (``strawberry.UNSET``) leaves the
    organization's slug alone; an explicit ``null`` or empty string is
    **refused**, because a slug cannot be cleared (it is NOT NULL, and the
    database rejects a blank one). The slug write and the branding upsert land
    in one transaction: an invalid, blank or colliding slug, or a
    field-validation failure anywhere else in this input, rejects the whole
    call and leaves the organization's slug unchanged.
    """

    app_name: str
    logo_url: str = ""
    primary_color: str = ""
    secondary_color: str = ""
    support_email: str = ""
    redirect_url: str = ""
    slug: str | None = strawberry.UNSET  # type: ignore[assignment]


@strawberry.type
class BrandingResult:
    """Represents resolved branding in the API response.

    Never includes secrets like support_email or redirect_url; those are for internal
    use only (email rendering, post-authentication redirect resolution).
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
    Excludes the branding row id, support_email, and redirect_url.
    """

    app_name: str
    logo_url: str
    primary_color: str
    secondary_color: str


@strawberry.type
class BrandingLogoUploadResult:
    """Signed upload payload for the ``branding_logos`` S3Direct destination.

    ``upload_url`` is a complete SigV4 presigned PUT URL — the caller uploads by
    PUTting the file body straight to it with a matching Content-Type and no
    other headers. No AWS credentials ever reach a partner-API caller, unlike
    the shipped s3direct signing view (``POST /s3direct/get_upload_params/``),
    which this mutation exists to serve as a REST-reachable equivalent of.
    Authorized by the branding eligibility helper (acting organization is
    parentless and holds ``white_label_branding``), not by the destination's own
    ``auth`` callable — see the plan's Logo upload path guiding decision.
    """

    object_key: str
    upload_url: str
    expires_in: int


@strawberry.input
class CreateScopedSystemUserInput:
    """Input for minting a provider-scoped Public API token.

    scoped_to_user_id is a User id. Internally it is resolved to the user's active
    OrganizationMembership in the caller's organization and the membership FK is stored.
    available_resources must be a non-empty list of resources drawn from the
    PROVIDER_SCOPED_RESOURCES allow-list.
    """

    integration_name: str
    scoped_to_user_id: int
    available_resources: list[str]


@strawberry.type
class CreateScopedSystemUserResult:
    """Result of minting a provider-scoped Public API token.

    token is the plaintext token — exposed exactly once and never persisted.
    scoped_to_user_id is the User id of the provider whose data this token may access
    (resolved internally to an OrganizationMembership for storage).
    """

    id: int
    integration_name: str
    is_active: bool
    available_resources: list[str]
    scoped_to_user_id: int
    token: str


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
