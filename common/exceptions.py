from vinta_orgs.exceptions import OrganizationCannotBeUpdatedError


class CommonError(Exception):
    """Base exception for common app errors"""

    pass


class OrganizationRequiredError(CommonError):
    def __init__(self, message="`organization` is required to create an instance."):
        super().__init__(message)


# Re-exported, not redefined. Package ``0.4.0`` made organization ownership
# immutable on existing rows and raises *its own*
# ``OrganizationCannotBeUpdatedError`` from all five write paths --
# ``save()``, ``QuerySet.update()``, ``bulk_update()``, ``update_or_create()``
# and conflict-updating ``bulk_create()``. This module used to declare a
# same-named ``CommonError`` subclass raised by a local ``update()`` override;
# two unrelated hierarchies sharing a name meant an ``except`` written against
# one silently missed the other, and the local raise fired *before* delegating,
# which made the package's ``unsafe_organization_update=True`` opt-in
# unreachable through ``.update()``. Both are gone: this name is now an alias
# for the single class every write path raises.
#
# Why the rule exists at all: moving a row between tenants is not an operation
# this codebase has. A row's organization is decided when it is created, and
# every relation it holds -- calendar, event, membership -- points inside the
# same organization. Relocating the row leaves each of those relations dangling
# across the tenant boundary, where an organization-safe join reads them as
# missing rather than as an error. A data migration that genuinely must
# re-stamp rows passes ``unsafe_organization_update=True`` at that call site;
# there is no blanket setting.
__all__ = [
    "CommonError",
    "OrganizationCannotBeUpdatedError",
    "OrganizationRequiredError",
]
