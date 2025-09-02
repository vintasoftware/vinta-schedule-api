from typing import Annotated

import strawberry
from dependency_injector.wiring import Provide, inject
from graphql import GraphQLError

from public_api.models import SystemUser
from public_api.services import PublicAPIAuthService


@strawberry.type
class AuthPayload:
    token_valid: bool


@strawberry.type
class Mutation:
    @inject
    def __init__(
        self,
        *args,
        public_api_auth_service: Annotated[
            PublicAPIAuthService, Provide["public_api_auth_service"]
        ],
        **kwargs,
    ):
        self.public_api_auth_service = public_api_auth_service
        super().__init__(*args, **kwargs)

    @strawberry.mutation
    def check_token(
        self,
        system_user_id: str,
        token: str,
    ) -> AuthPayload:
        try:
            system_user, authenticated = self.public_api_auth_service.check_system_user_token(
                system_user_id, token
            )
        except SystemUser.DoesNotExist as e:
            raise GraphQLError("System user does not exist") from e
        if not system_user or not authenticated:
            raise GraphQLError("Invalid credentials")

        return AuthPayload(token_valid=True)  # type: ignore
