"""Pointing a test at particular provider credentials.

``settings/base.py`` still reads the same env vars it always did --
``STRIPE_SECRET_KEY``, ``MERCADOPAGO_ACCESS_TOKEN`` and friends -- but it now
uses them only to *assemble* ``VINTA_BILLING['PROVIDERS']``, which is what the
engine actually reads (``vinta_billing.conf.get_provider_config``). Assigning
the top-level name after startup therefore changes nothing: the dict was built
at import time and nobody rereads the source setting.

That is a silent failure, not a loud one. A test that sets
``settings.STRIPE_PUBLISHABLE_KEY = "pk_test"`` and then asserts the endpoint
returns it fails with a confusing diff against whatever the real environment
happens to carry; a test that sets a *secret* and asserts a refusal can just as
easily pass for the wrong reason. So the rewrite goes through one helper rather
than being spelled out per test.
"""

from __future__ import annotations

import copy
from typing import Any

from django.conf import settings as django_settings

from vinta_billing.provider_slugs import MERCADOPAGO, STRIPE


#: The top-level settings name each test used to assign, mapped to the
#: ``VINTA_BILLING['PROVIDERS'][slug]`` entry that name now feeds. Keyed by the
#: old name on purpose: it keeps a retargeted test readable as the same test.
PROVIDER_CREDENTIAL_KEYS: dict[str, tuple[str, str]] = {
    "STRIPE_SECRET_KEY": (STRIPE, "API_KEY"),
    "STRIPE_WEBHOOK_SECRET": (STRIPE, "WEBHOOK_SECRET"),
    "STRIPE_PUBLISHABLE_KEY": (STRIPE, "PUBLISHABLE_KEY"),
    "MERCADOPAGO_ACCESS_TOKEN": (MERCADOPAGO, "ACCESS_TOKEN"),
    "MERCADOPAGO_WEBHOOK_SECRET": (MERCADOPAGO, "WEBHOOK_SECRET"),
    "MERCADOPAGO_PUBLIC_KEY": (MERCADOPAGO, "PUBLIC_KEY"),
}


def with_site_domain(site_domain: str | None) -> dict[str, Any]:
    """A copy of ``VINTA_BILLING`` whose ``SITE_DOMAIN`` is ``site_domain``.

    For ``@override_settings(VINTA_BILLING=with_site_domain(...))``, which is
    what ``@override_settings(SITE_DOMAIN=...)`` has to become.
    ``vinta_billing.conf.get_site_domain`` reads ``VINTA_BILLING['SITE_DOMAIN']``
    first and only falls back to the top-level ``SITE_DOMAIN``; this project sets
    the former (from the latter, at import), so overriding the top-level name
    alone changes nothing the adapters read -- and a test asserting that a
    *missing* domain raises would pass or fail for the wrong reason.
    """
    overrides = copy.deepcopy(django_settings.VINTA_BILLING)
    overrides["SITE_DOMAIN"] = site_domain
    return overrides


#: Matched against the refusal raised when no site domain is configured. The
#: host adapters used to say "MercadoPagoAdapter requires SITE_DOMAIN"; the
#: package's say "... VINTA_BILLING['SITE_DOMAIN'] (or a top-level SITE_DOMAIN
#: setting) must be set", per adapter, and ``urls_helpers.absolute_url`` says it
#: differently again. ``SITE_DOMAIN`` is the substring all of them share, and it
#: is the name the caller has to act on.
MISSING_SITE_DOMAIN_MESSAGE = "SITE_DOMAIN"


def no_site_domain() -> dict[str, Any]:
    """``override_settings(**no_site_domain())`` -- for the tests that assert a
    deployment with no site domain refuses to build a callback URL.

    Both names, because ``vinta_billing.conf.get_site_domain`` reads
    ``VINTA_BILLING['SITE_DOMAIN']`` and falls back to the top-level
    ``SITE_DOMAIN``. Clearing one leaves the other answering, and the test would
    then pass or fail on the wrong setting.
    """
    return {"VINTA_BILLING": with_site_domain(None), "SITE_DOMAIN": None}


def billing_settings(*, default_provider: str | None = None, **credentials: str) -> dict[str, Any]:
    """A copy of ``VINTA_BILLING`` with the named credentials replaced.

    ``credentials`` are spelled with the old top-level setting names -- e.g.
    ``STRIPE_PUBLISHABLE_KEY="pk_test"`` -- and land in the right
    ``PROVIDERS`` entry. Deep-copied so a test can never mutate the real
    settings dict in place, which would leak into every later test in the
    process *and* go unnoticed by ``vinta_billing.conf``'s cache (it compares
    the dict by identity, so an in-place mutation is invisible to it).

    :raises KeyError: for a credential name outside
        :data:`PROVIDER_CREDENTIAL_KEYS` -- a typo would otherwise configure
        nothing and leave the test asserting against the ambient environment.
    """
    overrides = copy.deepcopy(django_settings.VINTA_BILLING)
    if default_provider is not None:
        overrides["DEFAULT_PROVIDER"] = default_provider
    for name, value in credentials.items():
        slug, key = PROVIDER_CREDENTIAL_KEYS[name]
        overrides["PROVIDERS"].setdefault(slug, {})[key] = value
    return overrides


def use_providers(
    settings: Any, *, default_provider: str | None = None, **credentials: str
) -> None:
    """Apply :func:`billing_settings` through pytest-django's ``settings`` fixture.

    Assignment, not mutation: it fires ``setting_changed``, which is what
    invalidates ``vinta_billing.conf``'s cache and what restores the previous
    value at teardown.
    """
    settings.VINTA_BILLING = billing_settings(default_provider=default_provider, **credentials)
