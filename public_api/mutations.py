from dataclasses import dataclass
from typing import Annotated, cast

import strawberry
from dependency_injector.wiring import Provide, inject
from graphql import GraphQLError

from public_api.models import SystemUser
from public_api.services import PublicAPIAuthService


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


@strawberry.type
class Mutation:
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
