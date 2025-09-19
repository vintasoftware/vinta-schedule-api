class AccountError(Exception):
    """Base exception for account related errors"""

    pass


class UserNotAuthenticatedError(AccountError):
    def __init__(self, message="User must be authenticated to generate an access token."):
        super().__init__(message)
