"""Tests for how ``CSRF_TRUSTED_ORIGINS`` is built, and for the settings modules
that have to build it.

Headless social login checks ``callback_url`` with allauth's
``AccountAdapter.is_safe_url``. That method accepts the request host, the hosts in
``ALLOWED_HOSTS``, and the hosts in ``CSRF_TRUSTED_ORIGINS``. ECS sets
``ALLOWED_HOSTS`` to the API hostname alone, where Render used to set ``*``, so every
SPA OAuth callback is rejected unless the frontend origin appears in
``CSRF_TRUSTED_ORIGINS``.

``staging.py`` and ``production.py`` override ``FRONTEND_BASE_URL`` after
``from .base import *``, so each one recomputes the setting. That star import is also
why the helper's name cannot start with an underscore: ``import *`` skips such names,
and the ``NameError`` would appear at deploy time rather than in CI, because no test
imports the deployed settings modules. That is what
``test_deployed_settings_use_no_private_names_from_base`` checks.

The setting comes from ``FRONTEND_BASE_URL`` alone, and the helper raises on a value
that would give away more than intended. CORS_ALLOWED_ORIGINS is left out on purpose,
since one environment variable would otherwise open up CORS, CSRF, and the OAuth
redirect list together. A wildcard host is rejected, and plain http is rejected for
every host but a local development one.
"""

from __future__ import annotations

import ast
from pathlib import Path

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured

import pytest

from vinta_schedule_api.settings.base import build_csrf_trusted_origins


SETTINGS_DIR = Path(__file__).resolve().parents[2] / "vinta_schedule_api" / "settings"
DEPLOYED_SETTINGS_MODULES = ("staging.py", "production.py")


def test_frontend_base_url_is_trusted():
    assert build_csrf_trusted_origins("https://schedule-staging.vintasoftware.com") == [
        "https://schedule-staging.vintasoftware.com"
    ]


def test_path_and_trailing_slash_are_stripped_to_the_origin():
    assert build_csrf_trusted_origins("https://app.example.com/dashboard/") == [
        "https://app.example.com"
    ]


def test_port_is_part_of_the_origin():
    assert build_csrf_trusted_origins("http://localhost:3000") == ["http://localhost:3000"]


@pytest.mark.parametrize("hostname", ["localhost", "127.0.0.1", "[::1]"])
def test_cleartext_is_allowed_for_local_development_hosts(hostname):
    assert build_csrf_trusted_origins(f"http://{hostname}:3000") == [f"http://{hostname}:3000"]


@pytest.mark.parametrize("candidate", ["", "   ", "app.example.com", "ftp://app.example.com"])
def test_malformed_origin_is_rejected(candidate):
    """Raises instead of returning an empty list. An empty CSRF_TRUSTED_ORIGINS lets
    the app start normally and then rejects every social login callback in
    production."""
    with pytest.raises(ImproperlyConfigured):
        build_csrf_trusted_origins(candidate)


def test_wildcard_host_is_rejected():
    """django-cors-headers ignores a wildcard origin. Django's CSRF check accepts one.

    A value copied between the two settings therefore looks like it does nothing for
    CORS while it hands CSRF trust, and an OAuth redirect slot, to every subdomain.
    """
    with pytest.raises(ImproperlyConfigured):
        build_csrf_trusted_origins("https://*.vintasoftware.com")


def test_cleartext_is_rejected_for_a_non_local_host():
    """An http origin would be trusted for CSRF and accepted as an OAuth redirect
    target over cleartext. Only developer-machine hosts are allowed to do that."""
    with pytest.raises(ImproperlyConfigured):
        build_csrf_trusted_origins("http://schedule.vintasoftware.com")


def test_cors_origins_do_not_widen_the_setting():
    """CORS_ALLOWED_ORIGINS is used for CORS and nothing else.

    If it were read here as well, adding one vendor origin to CORS would also give
    that origin CSRF trust and a slot in allauth's list of OAuth redirect targets.
    """
    frontend_origin = "https://schedule.vintasoftware.com"
    vendor_origin = "https://widget.vendor.example"

    origins = build_csrf_trusted_origins(frontend_origin)

    assert origins == [frontend_origin]
    assert vendor_origin not in origins


def test_active_settings_trust_the_frontend_origin():
    """The invariant the OAuth callback depends on, asserted on live settings."""
    assert django_settings.FRONTEND_BASE_URL in django_settings.CSRF_TRUSTED_ORIGINS


def _module_level_assignment_lines(module: ast.Module, name: str) -> list[int]:
    """Line numbers where ``name`` is assigned at the top level of the module."""
    lines = []
    for node in module.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("module_name", DEPLOYED_SETTINGS_MODULES)
def test_deployed_settings_recompute_csrf_trusted_origins(module_name):
    """The regression itself: a module that overrides FRONTEND_BASE_URL and stops there.

    ``from .base import *`` copies the value base.py computed from the local dev
    frontend. Overriding FRONTEND_BASE_URL afterwards does not update it, so the real
    SPA origin is missing and allauth rejects every social login callback. Nothing else
    in the suite notices, because no test imports these modules.
    """
    module = ast.parse((SETTINGS_DIR / module_name).read_text())

    frontend_lines = _module_level_assignment_lines(module, "FRONTEND_BASE_URL")
    csrf_lines = _module_level_assignment_lines(module, "CSRF_TRUSTED_ORIGINS")

    assert frontend_lines, (
        f"{module_name} no longer overrides FRONTEND_BASE_URL. If that is intended, "
        "it inherits base.py's CSRF_TRUSTED_ORIGINS and this test should go."
    )
    assert csrf_lines, (
        f"{module_name} overrides FRONTEND_BASE_URL at line {frontend_lines[0]} but "
        "never recomputes CSRF_TRUSTED_ORIGINS, so it keeps the origin base.py derived "
        "from the local dev frontend and every social login callback is rejected."
    )
    assert max(csrf_lines) > max(frontend_lines), (
        f"{module_name} assigns CSRF_TRUSTED_ORIGINS at line {max(csrf_lines)}, before "
        f"the FRONTEND_BASE_URL override at line {max(frontend_lines)}, so it derives "
        "the setting from the value it is about to replace."
    )


@pytest.mark.parametrize("module_name", DEPLOYED_SETTINGS_MODULES)
def test_deployed_settings_use_no_private_names_from_base(module_name):
    """``from .base import *`` skips names that start with an underscore.

    A deployed settings module that reads one raises ``NameError`` when it is imported,
    which happens on the ECS task at boot, after CI has passed. So every such name
    these modules read has to be defined in the module itself.
    """
    module = ast.parse((SETTINGS_DIR / module_name).read_text())

    defined: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            defined.add(node.name)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            defined.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

    read_from_base = {
        node.id
        for node in ast.walk(module)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id.startswith("_")
        and not node.id.startswith("__")
        and node.id not in defined
    }

    assert not read_from_base, (
        f"{module_name} reads {sorted(read_from_base)} from a star import, but "
        "`from .base import *` skips underscore-prefixed names: this raises NameError "
        "at deploy time. Rename the name in base.py without the leading underscore."
    )
