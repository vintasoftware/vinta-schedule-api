from rest_framework.exceptions import PermissionDenied, ValidationError


class DuplicateInvitationError(ValidationError):
    default_detail = "An active invitation for this email already exists."
    default_code = "duplicate_invitation"


class NoServiceAccountConfiguredError(ValidationError):
    default_detail = (
        "Configure a Google service account for this organization before syncing rooms."
    )
    default_code = "no_service_account_configured"


class InvalidInvitationTokenError(ValidationError):
    default_detail = "Invalid or expired token"
    default_code = "invalid_invitation_token"


class InvitationNotFoundError(ValidationError):
    default_detail = "Invitation does not exist"
    default_code = "invitation_not_found"


class UserAlreadyHasMembershipError(ValidationError):
    default_detail = "User is already a member of this organization."
    default_code = "user_already_has_membership"


class OrganizationSlugCollisionError(ValidationError):
    """Raised when organization creation lost the race for its derived slug
    repeatedly.

    ``OrganizationService.create_organization`` derives a slug from the name,
    then inserts; a concurrent creation can claim the same value in between. The
    insert is retried on a fresh savepoint with a re-derived slug, so reaching
    this means several concurrent callers all lost in a row -- a 400 the client
    can retry, rather than the raw ``IntegrityError`` that would otherwise
    escape and abort the whole creation transaction.
    """

    default_detail = "Could not allocate a unique slug for this organization. Please retry."
    default_code = "organization_slug_collision"


class OrganizationHasParentBrandingError(PermissionDenied):
    """Raised by the branding write gate (``organizations.permissions.
    evaluate_branding_write_gate``) when the acting organization has a parent.

    Permanent refusal (spec Use-case 5): branding within a hierarchy belongs to
    the reseller alone, on every write surface, no matter which entry point the
    request came through. Distinguished from the other two gate refusals so a
    caller never mistakes this for a fixable billing/onboarding state.
    """

    default_detail = (
        "This organization has a parent organization and cannot manage its own "
        "branding. Branding for organizations inside a hierarchy is controlled "
        "by the reseller organization above them."
    )
    default_code = "branding_organization_has_parent"


class BrandingEntitlementRequiredError(PermissionDenied):
    """Raised by the branding write gate when the acting organization does not
    hold the ``white_label_branding`` entitlement -- a billing state, fixable
    by upgrading, unlike ``OrganizationHasParentBrandingError``."""

    default_detail = "This organization's plan does not include white-label branding."
    default_code = "branding_entitlement_required"


class OrganizationSlugRequiredForBrandingError(PermissionDenied):
    """Raised by the branding write gate when the acting organization is
    otherwise eligible but has not picked a public slug yet -- the "one step
    away" refusal (spec: "Eligible org with no public identifier yet")."""

    default_detail = "Pick a public slug for this organization before configuring branding."
    default_code = "branding_slug_required"


class BrandingLogoUploadRejectedError(Exception):
    """Raised when a requested branding-logo upload violates the ``branding_logos``
    S3Direct destination's content-type allowlist or size limit.

    Plain ``Exception`` (not a DRF ``ValidationError``) because its only caller
    today is the GraphQL signing mutation (``public_api.mutations.Mutation.
    create_branding_logo_upload``), which maps it to a ``GraphQLError`` itself.
    The message names the specific rule broken.
    """


class SlugDerivationError(RuntimeError):
    """Raised when no free, valid slug could be derived within the attempt budget.

    A ``RuntimeError`` rather than a DRF ``ValidationError``: nothing the caller
    submitted is wrong. Reaching the budget means either every candidate the
    generator produced was already taken -- which needs a namespace crowded far
    past anything ``organizations.slug_generation`` is sized for -- or
    ``validate_organization_slug`` is rejecting the shape the generator emits.
    Both are bugs here, not user input to be reported back over the API.
    """
