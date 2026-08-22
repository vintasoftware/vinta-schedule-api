"""``payments.notification_contexts`` -- the dunning/usage-warning render path.

``approaching_limit_context`` / ``limit_reached_context`` moved from
``LimitedResource(key).label`` (raised ``ValueError`` for an unknown key) to
``resources.get(key).label`` (raises ``django.core.exceptions.ImproperlyConfigured``
-- see ``vinta_billing.registry.ResourceRegistry.get``) when Phase 6 replaced the
``LimitedResource`` enum with the ``payments.seams.resources`` registry.
``LimitedResource`` no longer exists in this codebase at all, so reverting to the
old form is not an option; this module pins the registry's exception instead,
since nothing previously exercised either function body.
"""

from django.core.exceptions import ImproperlyConfigured

import pytest

# Registration already happens at process start (``DICoreConfig.ready()``); see
# ``payments/tests/seams/test_resources.py`` for why this explicit import is kept
# anyway rather than relying on that wiring alone.
import payments.seams.resources  # noqa: F401,E402
from payments.notification_contexts import approaching_limit_context, limit_reached_context
from payments.seams.resource_keys import ORGANIZATION_MEMBERS


class TestApproachingLimitContext:
    def test_resolves_the_registered_resource_label(self):
        context = approaching_limit_context(
            organization_name="Acme",
            resource_key=ORGANIZATION_MEMBERS,
            current_usage=4,
            limit_value=5,
        )

        assert context["resource_label"] == "Organization members"

    def test_an_unregistered_resource_key_raises_improperly_configured(self):
        """See the module docstring: the registry's lookup failure, not the
        retired enum's ``ValueError``."""
        with pytest.raises(ImproperlyConfigured):
            approaching_limit_context(
                organization_name="Acme",
                resource_key="not_a_real_resource",
                current_usage=4,
                limit_value=5,
            )


class TestLimitReachedContext:
    def test_resolves_the_registered_resource_label(self):
        context = limit_reached_context(
            organization_name="Acme",
            resource_key=ORGANIZATION_MEMBERS,
            current_usage=5,
            limit_value=5,
        )

        assert context["resource_label"] == "Organization members"

    def test_an_unregistered_resource_key_raises_improperly_configured(self):
        with pytest.raises(ImproperlyConfigured):
            limit_reached_context(
                organization_name="Acme",
                resource_key="not_a_real_resource",
                current_usage=5,
                limit_value=5,
            )
