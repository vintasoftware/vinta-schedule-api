import re
from dataclasses import dataclass
from typing import Annotated, cast

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

import strawberry
from dependency_injector.wiring import Provide, inject
from graphql import GraphQLError

from calendar_integration.mutations import CalendarGroupMutations
from organizations.exceptions import UserAlreadyHasMembershipError
from organizations.models import Organization, OrganizationBranding, OrganizationMembership
from organizations.services import OrganizationService
from public_api.capabilities import assert_org_can_invite, assert_target_in_subtree
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from public_api.permissions import IsAuthenticated, OrganizationResourceAccess
from public_api.services import PublicAPIAuthService
from public_api.types import (
    BrandingResult,
    CreateInvitationInput,
    CreateInvitationResult,
    CreateOrganizationInput,
    CreateOrganizationResult,
    CreateSystemUserTokenInput,
    CreateSystemUserTokenResult,
    InvitationResult,
    OrganizationResult,
    UpdateBrandingInput,
    UpdateBrandingResult,
)


@dataclass
class MutationDependencies:
    public_api_auth_service: PublicAPIAuthService
    organization_service: OrganizationService


@inject
def get_mutation_dependencies(
    public_api_auth_service: Annotated[
        PublicAPIAuthService | None, Provide["public_api_auth_service"]
    ] = None,
    organization_service: Annotated[
        OrganizationService | None, Provide["organization_service"]
    ] = None,
) -> MutationDependencies:
    required_dependencies = [public_api_auth_service, organization_service]
    if any(dep is None for dep in required_dependencies):
        raise GraphQLError(
            f"Missing required dependency {', '.join([str(dep) for dep in required_dependencies if dep is None])}"
        )

    return MutationDependencies(
        public_api_auth_service=cast(PublicAPIAuthService, public_api_auth_service),
        organization_service=cast(OrganizationService, organization_service),
    )


@strawberry.type
class AuthPayload:
    token_valid: bool


@strawberry.input
class DeleteSystemUserInput:
    system_user_id: int


@strawberry.type
class DeleteSystemUserResult:
    success: bool
    error_message: str | None = None


@strawberry.type
class Mutation(CalendarGroupMutations):
    @strawberry.mutation
    def check_token(
        self,
        system_user_id: int,
        token: str,
    ) -> AuthPayload:
        deps = get_mutation_dependencies()

        try:
            system_user, authenticated = deps.public_api_auth_service.check_system_user_token(
                system_user_id, token
            )
        except SystemUser.DoesNotExist as e:
            raise GraphQLError("System user does not exist") from e
        if not system_user or not authenticated:
            raise GraphQLError("Invalid credentials")

        return AuthPayload(token_valid=True)  # type: ignore

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def delete_system_user(
        self,
        info: strawberry.Info,
        input: DeleteSystemUserInput,  # noqa: A002
    ) -> DeleteSystemUserResult:
        org = info.context.request.public_api_organization
        if not org:
            return DeleteSystemUserResult(success=False, error_message="Organization not found")

        try:
            system_user = SystemUser.objects.get(
                id=input.system_user_id,
                organization=org,
                deleted_at__isnull=True,
            )
        except SystemUser.DoesNotExist:
            return DeleteSystemUserResult(success=False, error_message="System user not found")

        if system_user.is_active:
            return DeleteSystemUserResult(
                success=False,
                error_message="System user must be inactive before deletion",
            )

        system_user.deleted_at = timezone.now()
        system_user.save(update_fields=["deleted_at"])

        return DeleteSystemUserResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_organization(
        self,
        info: strawberry.Info,
        input: CreateOrganizationInput,  # noqa: A002
    ) -> CreateOrganizationResult:
        """
        Create a child organization under the acting (reseller) organization.

        The mutation:
        1. Checks that the acting org has the can_invite_organizations flag (via assert_org_can_invite).
        2. Ensures no sibling with the same name already exists under the parent.
        3. Creates the child with parent=acting_org and can_invite_organizations=False.
        4. Returns the created organization's id and name.

        The token's OrganizationResourceAccess must include the ORGANIZATION resource.
        """
        acting_org = info.context.request.public_api_organization
        if not acting_org:
            raise GraphQLError("Organization not found")

        # Gate: check the org can invite before proceeding
        assert_org_can_invite(acting_org)

        # Validate no duplicate name under the same parent
        if Organization.objects.filter(parent=acting_org, name=input.name).exists():
            raise GraphQLError(
                f"An organization with name '{input.name}' already exists under this parent."
            )

        # Create the child org with parent=acting_org and can_invite_organizations=False
        try:
            child_org = Organization.objects.create(
                name=input.name,
                parent=acting_org,
                can_invite_organizations=False,
            )
        except IntegrityError as e:
            raise GraphQLError(
                f"An organization with name '{input.name}' already exists under this parent."
            ) from e

        return CreateOrganizationResult(
            organization=OrganizationResult(id=child_org.id, name=child_org.name)
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_invitation(
        self,
        info: strawberry.Info,
        input: CreateInvitationInput,  # noqa: A002
    ) -> CreateInvitationResult:
        """
        Create a pending organization invitation for an end-user (reseller bundle).

        The mutation:
        1. Checks the acting org has can_invite_organizations (via assert_org_can_invite).
        2. Validates organizationId is the acting org or a descendant (subtree guard).
        3. Checks the user is not already an active member of the target org.
        4. Creates (or resets) a pending OrganizationInvitation via OrganizationService.
        5. Sends the invitation email (Phase 3: sendEmail=true path only).
           Phase 4 will add the sendEmail=false branch returning the raw token.
        6. Returns the invitation with token=None and invite_url=None (email path).

        The token's OrganizationResourceAccess must include the INVITATION resource.
        """
        deps = get_mutation_dependencies()

        acting_org = info.context.request.public_api_organization
        if not acting_org:
            raise GraphQLError("Organization not found")

        # Gate: check the org can invite before proceeding
        assert_org_can_invite(acting_org)

        # Resolve the target organization
        try:
            target_org = Organization.objects.get(id=int(input.organization_id))
        except (Organization.DoesNotExist, ValueError) as e:
            raise GraphQLError(f"Organization with id '{input.organization_id}' not found.") from e

        # Tenant-isolation guard: target must be the acting org or a descendant
        assert_target_in_subtree(acting_org, target_org)

        # Already-active-member guard: reject if the email belongs to an existing member.
        # We check by email because the invitation itself creates the user (the user may not
        # exist yet).
        user_model = get_user_model()
        try:
            existing_user = user_model.objects.get(email=input.user_email)
        except user_model.DoesNotExist:
            existing_user = None

        if existing_user is not None:
            if OrganizationMembership.objects.filter(
                user=existing_user,
                organization=target_org,
                is_active=True,
            ).exists():
                raise GraphQLError(
                    UserAlreadyHasMembershipError.default_detail,
                )

        # Create (or reset) the pending invitation via OrganizationService.
        # invited_by=None because the public-API caller is a SystemUser, not a Django User.
        # first_name/last_name are empty strings — invite_user_to_organization creates (or
        # reuses) the user and stores names for email rendering only.
        #
        # Phase 4: when send_email=False the service suppresses the email and attaches the raw
        # token as invitation._raw_token (transient, never persisted in plaintext).
        invitation = deps.organization_service.invite_user_to_organization(
            email=input.user_email,
            first_name="",
            last_name="",
            organization=target_org,
            invited_by=None,
            role=input.role.to_model_role(),
            send_email=input.send_email,
        )

        raw_token: str | None = None
        invite_url: str | None = None

        if not input.send_email:
            # The service always attaches _raw_token; retrieve it once so it is not
            # inadvertently retained beyond this scope.
            raw_token = invitation._raw_token  # type: ignore[attr-defined]
            # Build the invite URL using the same template the branded email uses.
            # Phase 6+ may refine this URL from the reseller's return_url_allowlist once
            # OrganizationBranding is available; for now we use the same base as the email.
            url_template: str = getattr(settings, "HEADLESS_FRONTEND_URLS", {}).get(
                "account_accept_invitation", ""
            )
            invite_url = url_template.format(token=raw_token) if url_template else None

        return CreateInvitationResult(
            invitation=InvitationResult(
                id=invitation.id,
                email=invitation.email,
                expires_at=invitation.expires_at,
            ),
            token=raw_token,
            invite_url=invite_url,
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def create_system_user_token(
        self,
        info: strawberry.Info,
        input: CreateSystemUserTokenInput,  # noqa: A002
    ) -> CreateSystemUserTokenResult:
        """
        Mint a delegated Public API token for the reseller's subtree (reseller bundle).

        The mutation:
        1. Checks that the acting org has the can_invite_organizations flag (via assert_org_can_invite).
        2. Resolves the target org from organizationId and validates it is the acting org or
           a descendant (subtree guard — reuses assert_target_in_subtree).
        3. Validates that resources is non-empty and every item is a valid PublicAPIResources value.
           ORGANIZATION may be included to delegate the invite-orgs capability; it still cannot
           set the DB flag (that is DB/admin-only).
        4. Mints a SystemUser via PublicAPIAuthService.create_system_user and bulk-creates
           ResourceAccess rows for the requested resources (mirrors REST SystemUserTokenCreate).
        5. Returns { systemUserId, token } — plaintext token exposed once, never persisted.

        The token's OrganizationResourceAccess must include the SYSTEM_USER resource.
        """
        deps = get_mutation_dependencies()

        acting_org = info.context.request.public_api_organization
        if not acting_org:
            raise GraphQLError("Organization not found")

        # Gate: check the org can invite before proceeding
        assert_org_can_invite(acting_org)

        # Resolve the target organization
        try:
            target_org = Organization.objects.get(id=int(input.organization_id))
        except (Organization.DoesNotExist, ValueError) as e:
            raise GraphQLError(f"Organization with id '{input.organization_id}' not found.") from e

        # Tenant-isolation guard: target must be the acting org or a descendant
        assert_target_in_subtree(acting_org, target_org)

        # Validate resources: must be non-empty and all values must be valid PublicAPIResources
        if not input.resources:
            raise GraphQLError("resources must not be empty.")

        valid_values = {r.value for r in PublicAPIResources}
        invalid = [r for r in input.resources if r not in valid_values]
        if invalid:
            raise GraphQLError(
                f"Invalid resource(s): {', '.join(invalid)}. "
                f"Valid values are: {', '.join(sorted(valid_values))}."
            )

        # Mint the system user and persist ResourceAccess rows (mirrors REST create)
        try:
            with transaction.atomic():
                system_user, plaintext_token = deps.public_api_auth_service.create_system_user(
                    integration_name=input.integration_name,
                    organization=target_org,
                )
                # dict.fromkeys dedupes while preserving order; prevents constraint violations
                ResourceAccess.objects.bulk_create(
                    [
                        ResourceAccess(system_user=system_user, resource_name=resource_name)
                        for resource_name in dict.fromkeys(input.resources)
                    ]
                )
        except IntegrityError as e:
            raise GraphQLError(
                f"A token with integration_name '{input.integration_name}' already exists."
            ) from e

        return CreateSystemUserTokenResult(
            system_user_id=strawberry.ID(str(system_user.id)),
            token=plaintext_token,
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])
    def update_branding(
        self,
        info: strawberry.Info,
        input: UpdateBrandingInput,  # noqa: A002
    ) -> UpdateBrandingResult:
        """
        Update or create branding for the acting (reseller) organization.

        The mutation:
        1. Checks that the acting org has can_invite_organizations (via assert_org_can_invite).
        2. Validates primary_color and secondary_color format (#RRGGBB or #RRGGBBAA).
        3. Validates each entry in return_url_allowlist is a valid URL.
        4. Upserts OrganizationBranding on the acting org only (always keyed to acting_org).
        5. Returns the upserted branding row (without internal fields like support_email/allowlist).

        The token's OrganizationResourceAccess must include the BRANDING resource.
        """
        acting_org = info.context.request.public_api_organization
        if not acting_org:
            raise GraphQLError("Organization not found")

        # Gate: check the org can invite before proceeding
        assert_org_can_invite(acting_org)

        # Validate color format: #RRGGBB or #RRGGBBAA (6 or 8 hex chars after #)
        hex_pattern = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")

        if input.primary_color and not hex_pattern.match(input.primary_color):
            raise GraphQLError(
                f"Invalid primary_color format: '{input.primary_color}'. "
                "Expected #RRGGBB or #RRGGBBAA."
            )

        if input.secondary_color and not hex_pattern.match(input.secondary_color):
            raise GraphQLError(
                f"Invalid secondary_color format: '{input.secondary_color}'. "
                "Expected #RRGGBB or #RRGGBBAA."
            )

        # Validate return_url_allowlist entries are URLs
        allowlist = input.return_url_allowlist or []
        for url in allowlist:
            # Simple URL validation: must start with http:// or https://
            if not (url.startswith("http://") or url.startswith("https://")):
                raise GraphQLError(
                    f"Invalid URL in return_url_allowlist: '{url}'. "
                    "URLs must start with http:// or https://."
                )

        # Upsert branding on the acting org (always acts on acting org, never another org)
        branding, _ = OrganizationBranding.objects.update_or_create(
            organization=acting_org,
            defaults={
                "app_name": input.app_name,
                "logo_url": input.logo_url,
                "primary_color": input.primary_color,
                "secondary_color": input.secondary_color,
                "support_email": input.support_email,
                "return_url_allowlist": allowlist,
            },
        )

        # Return the branding without internal fields (no support_email, no allowlist)
        branding_result = BrandingResult(
            id=branding.id,
            app_name=branding.app_name,
            logo_url=branding.logo_url,
            primary_color=branding.primary_color,
            secondary_color=branding.secondary_color,
        )

        return UpdateBrandingResult(branding=branding_result)
