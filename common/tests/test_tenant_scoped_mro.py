"""``OrganizationScopedAPIViewMixin`` wins the MRO, on every view that uses it.

Phase 3.5 handed the resolution seam to the package. The package owns it through
two overrides -- ``perform_authentication`` (which resolves and binds, between
authentication and ``check_permissions``) and ``dispatch`` (whose ``finally``
releases the binding on every exit path). Both are only reached if the mixin
comes **before** ``rest_framework.views.APIView`` in the class's MRO. Put a DRF
base class first by accident and Python silently picks ``APIView``'s versions:
nothing raises, nothing 500s, and every request served by that view resolves no
organization while its permission classes and querysets carry on asking for one.

Two views are checked, and the second is the one that catches a regression a
hand-written list would miss:

1. Each base viewset declared in ``common/utils/view_utils.py``. These are
   abstract, so no request-level test reaches them directly.
2. Every **routed** view class in the project's URL conf that inherits
   ``TenantScopedViewMixin`` -- which is where the hand-rolled users live
   (``OrganizationBrandingView``, ``SystemUserTokenViewSet``, the billing
   viewsets, ...). A new one added tomorrow with its bases the wrong way round
   fails here without anybody remembering to list it.
"""

from typing import Any

from django.urls import URLPattern, URLResolver, get_resolver

import pytest
from rest_framework.views import APIView
from vinta_orgs.drf import OrganizationScopedAPIViewMixin

from common.utils import view_utils
from common.utils.view_utils import TenantScopedViewMixin


#: The abstract bases every internal REST endpoint is expected to be built from.
BASE_VIEWSETS: tuple[type, ...] = tuple(
    obj
    for name, obj in vars(view_utils).items()
    if isinstance(obj, type)
    and name.endswith("VintaScheduleModelViewSet")
    and issubclass(obj, TenantScopedViewMixin)
)


def _routed_tenant_scoped_views() -> dict[str, type]:
    """Every routed class in the URL conf that inherits ``TenantScopedViewMixin``.

    ``APIView.as_view`` and ``ViewSetMixin.as_view`` both stamp ``view.cls`` onto
    the closure they return, which is the only way back from a resolved route to
    the class that serves it.

    Called from inside the tests rather than at import time: the URL conf
    registers custom path converters from an app's ``ready()``, so resolving it
    during collection raises ``ImproperlyConfigured``.
    """
    found: dict[str, type] = {}

    def walk(patterns: Any) -> None:
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns)
            elif isinstance(pattern, URLPattern):
                view_class = getattr(pattern.callback, "cls", None)
                if isinstance(view_class, type) and issubclass(view_class, TenantScopedViewMixin):
                    found[f"{view_class.__module__}.{view_class.__qualname__}"] = view_class

    walk(get_resolver().url_patterns)
    return found


def _defining_class(view_class: type, method_name: str) -> type:
    """The class Python actually resolves ``method_name`` to for ``view_class``."""
    for candidate in view_class.__mro__:
        if method_name in vars(candidate):
            return candidate
    raise AssertionError(f"{view_class.__name__} does not define {method_name} anywhere")


def test_the_base_viewsets_were_discovered() -> None:
    """Guards the parametrisation below against silently becoming empty."""
    assert len(BASE_VIEWSETS) >= 8, BASE_VIEWSETS


def test_the_routed_views_were_discovered() -> None:
    """Same guard for the URL-conf sweep.

    An import error or a renamed mixin would otherwise turn the sweep into a
    no-op that passes.
    """
    routed = _routed_tenant_scoped_views()

    assert len(routed) >= 20, sorted(routed)


@pytest.mark.parametrize("view_class", BASE_VIEWSETS, ids=lambda cls: cls.__name__)
def test_a_base_viewset_resolves_the_packages_overrides(view_class: type) -> None:
    assert _wrong_mro(view_class) == []


def test_every_routed_view_resolves_the_packages_overrides() -> None:
    """The hand-rolled users, swept rather than listed.

    One test rather than a parametrisation because the URL conf cannot be
    resolved at collection time (see ``_routed_tenant_scoped_views``); the
    failure message names every offender, so the diagnostic is not lost.
    """
    offenders = {
        path: problems
        for path, view_class in _routed_tenant_scoped_views().items()
        if (problems := _wrong_mro(view_class))
    }

    assert offenders == {}


def _wrong_mro(view_class: type) -> list[str]:
    """Everything ``view_class`` gets wrong about the package's two overrides."""
    problems: list[str] = []
    mro = view_class.__mro__

    if mro.index(OrganizationScopedAPIViewMixin) > mro.index(APIView):
        problems.append("OrganizationScopedAPIViewMixin comes after APIView")
    for method_name in ("perform_authentication", "dispatch"):
        owner = _defining_class(view_class, method_name)
        if owner is not OrganizationScopedAPIViewMixin:
            problems.append(f"{method_name} resolves to {owner.__name__}")

    return problems


def test_our_subclass_adds_only_what_is_ours() -> None:
    """``TenantScopedViewMixin`` overrides the two package hooks it is allowed to.

    ``get_organization_slug`` (our header) and ``resolve_organization`` (our
    refusal bodies) are the whole of this repo's contribution. Reintroducing an ``initial``, ``dispatch`` or
    ``perform_authentication`` override here is the mistake this pins: it would
    take the seam back off the package, which is what Phase 3.5 handed over.
    """
    overridden = {
        name
        for name in vars(TenantScopedViewMixin)
        if not name.startswith("__") and hasattr(OrganizationScopedAPIViewMixin, name)
    }

    assert overridden == {"get_organization_slug", "resolve_organization"}
    assert "initial" not in vars(TenantScopedViewMixin)
