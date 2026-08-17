from typing import Any

from common.managers import OrganizationScopedManager
from public_api.querysets import SystemUserQuerySet


# ``from_queryset`` rather than a hand-rolled ``get_queryset`` returning
# ``SystemUserQuerySet(self.model, using=self._db)``. Building the queryset
# directly skips ``OrganizationScopedManager.get_queryset`` entirely, so
# ``objects`` would *look* scoped while reading every tenant -- the defect
# found in all 12 ``calendar_integration`` managers.
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

    @staticmethod
    def _names_an_organization(kwargs: dict[str, Any] | None) -> bool:
        """``organization=None`` counts as naming one on this model.

        The package tests the *value*
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

        **The same rule, one frame lower, is what grants that exception.**
        ``create()`` hands these keyword arguments to ``Model.__init__``, and
        ``SystemUser.__init__`` records "the organization was written as
        ``None``" as ``_organization_is_deliberately_none``; ``save()`` skips the
        mixin's stamp-or-raise for that marker and for nothing else. So the
        exemption is keyed on the caller's stated intent rather than on the
        absence of a value, and a forgotten ``organization=`` raises here as it
        would on any other scoped model. It is recorded in ``__init__`` rather
        than here so that ``get_or_create`` / ``update_or_create`` (which build
        the instance on the queryset, below this method) and a direct
        ``SystemUser(organization=None, ...)`` are covered by the same line.

        **A ``staticmethod`` naming its parent rather than calling ``super()``**,
        because that is the shape package ``0.4.0`` gave
        ``OrganizationScopedManagerMixin._names_an_organization``, and the
        zero-argument ``super()`` needs a first positional argument a static
        method does not have.
        """
        organization_kwargs = OrganizationScopedManager._ORGANIZATION_KWARGS
        if kwargs and any(name in kwargs for name in organization_kwargs):
            return True
        return OrganizationScopedManager._names_an_organization(kwargs)
