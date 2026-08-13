from typing import Any

from common.managers import OrganizationScopedManager
from public_api.querysets import SystemUserQuerySet


# ``from_queryset`` rather than a hand-rolled ``get_queryset`` returning
# ``SystemUserQuerySet(self.model, using=self._db)``. Building the queryset
# directly skips ``OrganizationScopedManager.get_queryset`` entirely, so
# ``objects`` would *look* scoped while reading every tenant -- the defect
# Phase 2a found in all 12 ``calendar_integration`` managers.
_SystemUserManagerBase = OrganizationScopedManager.from_queryset(SystemUserQuerySet)


class SystemUserManager(_SystemUserManagerBase):  # type: ignore[misc,valid-type]
    """Manager for SystemUser with domain-specific query methods.

    ``live()`` is copied off :class:`~public_api.querysets.SystemUserQuerySet` by
    ``from_queryset``. Note that an **org-less** ``SystemUser``
    (``organization=None``, the admin-minted global credential) is invisible to
    every *read* through this manager, scoped or not, because the implicit scope
    is an equality on ``organization_id``. Read those through
    ``SystemUser.original_manager``.
    """

    def _names_an_organization(self, kwargs: dict[str, Any] | None) -> bool:
        """``organization=None`` counts as naming one on this model.

        :class:`~common.managers.OrganizationScopedManager` tests the *value*
        (``kwargs.get(name) is not None``) because on every other scoped model a
        ``None`` there is an argument the caller forgot, and routing it through
        the scoped queryset is how it gets an organization -- or a clear error.
        ``SystemUser`` is the exception: ``organization`` is nullable and
        ``organization=None`` is the org-less credential with access to every
        organization, so the caller *has* said which organization it wants
        (none). Testing for the key's presence instead lets
        ``create(organization=None, ...)`` -- the admin's supported org-less
        path, via ``PublicAPIAuthService.create_system_user`` -- reach the row
        it means to write. ``SystemUser.save()`` carries the matching exception
        for the stamp-or-raise step.
        """
        if kwargs and any(name in kwargs for name in self._ORGANIZATION_KWARGS):
            return True
        return super()._names_an_organization(kwargs)
