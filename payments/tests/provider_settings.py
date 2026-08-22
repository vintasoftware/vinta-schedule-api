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

    Also assigns the top-level ``settings.DEFAULT_PAYMENT_PROVIDER`` when
    ``default_provider`` is given. ``payments.migrations.0018_repoint_subscription_
    payment_provider.repoint_to_organization_provider`` is frozen at the point it
    was written and still reads that top-level name directly, not
    ``VINTA_BILLING['DEFAULT_PROVIDER']`` -- a data migration doesn't get to
    "modernize" itself when the setting it reads is later renamed. Tests exercising
    that migration need both names set to the same value.
    """
    settings.VINTA_BILLING = billing_settings(default_provider=default_provider, **credentials)
    if default_provider is not None:
        settings.DEFAULT_PAYMENT_PROVIDER = default_provider
