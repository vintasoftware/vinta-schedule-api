from rest_framework.exceptions import AuthenticationFailed, ValidationError


class InvalidAuthorizationHeaderError(AuthenticationFailed):
    default_detail = "Invalid Authorization header format"
    default_code = "invalid_authorization_header"


class PublicAPIServiceUnavailableError(ValidationError):
    default_detail = "PublicAPIAuthService is not available"
    default_code = "public_api_service_unavailable"


class OnBehalfOfSwitchOnScopedTokenError(ValidationError):
    """``requires_on_behalf_of`` was turned on for a token scoped to a member.

    A member-scoped token ignores the ``X-Public-Api-On-Behalf-Of`` header, so
    with the switch on it could never make a write again.
    """

    default_code = "on_behalf_of_switch_on_scoped_token"

    def __init__(self) -> None:
        # Keyed by the field, so a REST client sees it as a field error.
        super().__init__(
            {
                "requires_on_behalf_of": [
                    "Only organization-wide and org-less tokens can require the "
                    "X-Public-Api-On-Behalf-Of header."
                ]
            }
        )
