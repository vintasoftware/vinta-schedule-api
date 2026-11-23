from rest_framework.exceptions import AuthenticationFailed, ValidationError

from public_api.constants import ON_BEHALF_OF_HEADER, OnBehalfOfErrorCode


class InvalidAuthorizationHeaderError(AuthenticationFailed):
    default_detail = "Invalid Authorization header format"
    default_code = "invalid_authorization_header"


class PublicAPIServiceUnavailableError(ValidationError):
    default_detail = "PublicAPIAuthService is not available"
    default_code = "public_api_service_unavailable"


#: Why a member-scoped token cannot turn on ``requires_on_behalf_of``. Shared by
#: the service error below and the admin form, so both say the same thing.
ORG_WIDE_ONLY_SWITCH_MESSAGE = (
    "Only organization-wide and org-less tokens can require the X-Public-Api-On-Behalf-Of header."
)


class OnBehalfOfSwitchOnScopedTokenError(ValidationError):
    """``requires_on_behalf_of`` was turned on for a token scoped to a member.

    A member-scoped token ignores the ``X-Public-Api-On-Behalf-Of`` header, so
    with the switch on it could never make a write again.
    """

    default_code = "on_behalf_of_switch_on_scoped_token"

    def __init__(self) -> None:
        # Keyed by the field, so a REST client sees it as a field error.
        super().__init__({"requires_on_behalf_of": [ORG_WIDE_ONLY_SWITCH_MESSAGE]})


class OnBehalfOfMemberError(Exception):
    """A mutation's ``X-Public-Api-On-Behalf-Of`` header was refused.

    ``code`` is the ``extensions.code`` the GraphQL error carries. The message
    never repeats the value that was sent.
    """

    code: OnBehalfOfErrorCode
    message: str

    def __init__(self) -> None:
        super().__init__(self.message)


class OnBehalfOfMemberInvalidError(OnBehalfOfMemberError):
    """The header is not a positive integer, or names no active member of the organization."""

    code = OnBehalfOfErrorCode.INVALID
    message = f"{ON_BEHALF_OF_HEADER} does not name an active member of this organization."


class OnBehalfOfMemberRequiredError(OnBehalfOfMemberError):
    """The token requires the header on mutations, and the request did not send it."""

    code = OnBehalfOfErrorCode.REQUIRED
    message = f"This token requires the {ON_BEHALF_OF_HEADER} header on mutations."
