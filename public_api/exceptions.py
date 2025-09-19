from rest_framework.exceptions import AuthenticationFailed, ValidationError


class InvalidAuthorizationHeaderError(AuthenticationFailed):
    default_detail = "Invalid Authorization header format"
    default_code = "invalid_authorization_header"


class PublicAPIServiceUnavailableError(ValidationError):
    default_detail = "PublicAPIAuthService is not available"
    default_code = "public_api_service_unavailable"
