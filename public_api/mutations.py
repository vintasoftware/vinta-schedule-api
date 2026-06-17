from dataclasses import dataclass
from typing import Annotated, cast

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone

import strawberry
from allauth.account.models import EmailAddress
from dependency_injector.wiring import Provide, inject
from graphql import GraphQLError

from calendar_integration.mutations import CalendarGroupMutations
from organizations.models import Organization
from public_api.capabilities import assert_org_can_invite
from public_api.models import SystemUser
from public_api.permissions import IsAuthenticated, OrganizationResourceAccess
from public_api.services import PublicAPIAuthService
from public_api.types import (
    CreateOrganizationInput,
    CreateOrganizationResult,
    CreateUserInput,
    CreateUserResult,
    OrganizationResult,
    UserResult,
)
from users.models import Profile


@dataclass
class MutationDependencies:
    public_api_auth_service: PublicAPIAuthService


@inject
def get_mutation_dependencies(
    public_api_auth_service: Annotated[
        PublicAPIAuthService | None, Provide["public_api_auth_service"]
    ] = None,
) -> MutationDependencies:
    required_dependencies = [public_api_auth_service]
    if any(dep is None for dep in required_dependencies):
        raise GraphQLError(
            f"Missing required dependency {', '.join([str(dep) for dep in required_dependencies if dep is None])}"
        )

    return MutationDependencies(
        public_api_auth_service=cast(PublicAPIAuthService, public_api_auth_service),
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
    def create_user(
        self,
        info: strawberry.Info,
        input: CreateUserInput,  # noqa: A002
    ) -> CreateUserResult:
        """
        Create a passwordless end-user (provisioned by a reseller).

        The mutation:
        1. Checks that the acting org has the can_invite_organizations flag (via assert_org_can_invite).
        2. Idempotent on email: if a User with that email exists, returns it (does NOT duplicate).
        3. Creates User + Profile with set_unusable_password() and email left UNVERIFIED.
        4. Returns the created user with id, email, first_name, and last_name.

        The token's OrganizationResourceAccess must include the USER resource.
        """
        user_model = get_user_model()

        acting_org = info.context.request.public_api_organization
        if not acting_org:
            raise GraphQLError("Organization not found")

        # Gate: check the org can invite before proceeding
        assert_org_can_invite(acting_org)

        # Idempotent on email: get-or-create pattern
        user, created = user_model.objects.get_or_create(
            email=input.email,
            defaults={},
        )

        # Guarantee a Profile exists (handles both newly created and profile-less existing users)
        profile, _ = Profile.objects.get_or_create(user=user)

        if created:
            # Only set unusable password and email if the user is newly created
            user.set_unusable_password()
            user.save(update_fields=["password"])

            # Set first_name and last_name on the Profile for the new user
            if input.first_name is not None:
                profile.first_name = input.first_name
            if input.last_name is not None:
                profile.last_name = input.last_name
            profile.save()

            # Create an EmailAddress entry with verified=False (email is unverified)
            EmailAddress.objects.create(
                user=user,
                email=user.email,
                verified=False,
                primary=True,
            )

        return CreateUserResult(
            user=UserResult(
                id=user.id,
                email=user.email,
                first_name=profile.first_name or None,
                last_name=profile.last_name or None,
            )
        )
