class CommonError(Exception):
    """Base exception for common app errors"""

    pass


class OrganizationRequiredError(CommonError):
    def __init__(self, message="`organization` is required to create an instance."):
        super().__init__(message)


class OrganizationCannotBeUpdatedError(CommonError):
    """Raised by ``common.querysets.OrganizationScopedQuerySet.update`` when a
    bulk ``UPDATE`` tries to write the ``organization`` column.

    Moving a row between tenants is not an operation this codebase has: a row's
    organization is decided when it is created, and every relation it holds --
    calendar, event, membership -- points inside the same organization. A bulk
    ``update(organization=...)`` would relocate the row and leave every one of
    those relations dangling across the tenant boundary, where an
    organization-safe join reads them as missing rather than as an error. There
    is no supported way to do it, so the statement is refused rather than
    half-supported.
    """

    def __init__(self, message="`organization` cannot be updated."):
        super().__init__(message)
