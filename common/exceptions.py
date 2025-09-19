class CommonError(Exception):
    """Base exception for common app errors"""

    pass


class OrganizationRequiredError(CommonError):
    def __init__(self, message="`organization` is required to create an instance."):
        super().__init__(message)
