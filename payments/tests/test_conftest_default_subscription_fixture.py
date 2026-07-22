"""The root ``conftest.py``'s autouse ``provision_default_subscription`` fixture
must build its ``SubscriptionService`` from the DI container, not hand-construct one,
matching what the rest of the billing tests use (see
``public_api/tests/test_system_user_limits.py``'s ``service`` fixture and
``payments/tests/test_prepaid_resource_coverage.py``'s ``_container()`` helper).

``SubscriptionService`` happens to take no injected dependencies today, so a
hand-constructed instance behaves identically to a container-built one -- this test
is about wiring, not behavior: it proves the fixture actually asks the container
(so a future dependency added to ``SubscriptionService`` -- and wired only via the
container -- is not silently missed here).
"""

from unittest.mock import MagicMock

import pytest
from model_bakery import baker

from organizations.models import Organization


@pytest.mark.django_db
class TestProvisionDefaultSubscriptionFixtureUsesTheContainer:
    def test_creating_an_organization_asks_the_container_for_the_subscription_service(self):
        from di_core.containers import container

        assert container is not None, "DI container is only assigned in DICoreConfig.ready()"

        mock_subscription_service = MagicMock()

        with container.subscription_service.override(mock_subscription_service):
            organization = baker.make(Organization, parent=None, can_invite_organizations=False)

        mock_subscription_service.create_subscription_for_organization.assert_called_once_with(
            organization
        )
