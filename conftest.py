import ipaddress as _ipaddress
import socket as _socket
from unittest.mock import MagicMock

import pytest
from rest_framework.test import APIClient


# Repairs the one hazard the package's seeded-group pattern creates on its own:
# a ``transaction=True`` test flushes ``auth_group`` / ``auth_group_permissions``
# on teardown, and ``flush`` does not replay data migrations, so the three
# groups ``organizations/migrations/0028_seed_permission_groups.py`` seeded
# vanish for the rest of that worker's session. The autouse
# ``seeded_organization_groups`` fixture this plugin provides reseeds them
# (via ``ORGANIZATION_GROUP_SEEDERS``, see ``vinta_schedule_api/settings/base.py``)
# before every test with database access. See ``vinta_orgs.testing`` for the
# full contract -- it must be a setup hook, not a teardown one, because the
# flush runs inside pytest-django's own finalizer, later than any conftest
# fixture's teardown could reach.
pytest_plugins = ["vinta_orgs.testing"]


_ALLOWED_NETWORK_HOSTS = {
    "127.0.0.1",
    "::1",
    "localhost",
    "0.0.0.0",
    "",
    # docker-compose service hostnames
    "db",
    "broker",
    "result",
    "floci",
    "mailpit",
}


def _network_host_allowed(host) -> bool:
    if host in _ALLOWED_NETWORK_HOSTS:
        return True
    try:
        ip = _ipaddress.ip_address(host)
    except ValueError:
        return False  # unknown hostname -> assume external -> block
    return not ip.is_global  # loopback/private allowed, public internet blocked


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    """Tests must not touch the public internet.

    Allow loopback + docker-compose service hosts (postgres/redis/etc.); any connect to a
    public address fails fast with a clear error instead of hanging the suite / CI runner.
    """
    real_connect = _socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, (tuple, list)) and address else None
        if not _network_host_allowed(host):
            raise RuntimeError(
                f"Blocked external network access in test: {address!r}. "
                "Mock the external client (see conftest.mock_external_calendar_clients)."
            )
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(_socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def mock_external_calendar_clients(monkeypatch):
    """Globally mock the external calendar provider clients so tests never hit their APIs.

    Covers the only external calendar APIs we consume:
      * Google Calendar  -> googleapiclient ``build`` + OAuth ``Credentials``/``Request``
      * Microsoft Outlook -> ``MSOutlookCalendarAPIClient`` (Graph)

    allauth's social-auth HTTP calls are caught by ``block_external_network``; tests that
    exercise those flows should mock the provider responses explicitly.
    """
    from calendar_integration.services.calendar_adapters import (
        google_calendar_adapter,
        ms_outlook_calendar_adapter,
    )

    # Google: build() returns a mock client; credentials never refresh over the network.
    # Configure the paginated list calls to return an empty page (no nextPageToken) so the
    # adapter's `while True` pagination loops terminate instead of spinning forever on a
    # truthy MagicMock token (which would OOM the worker).
    google_client = MagicMock(name="google_calendar_client")
    _empty_google_page = {"items": []}
    google_client.events.return_value.list.return_value.execute.return_value = _empty_google_page
    google_client.calendarList.return_value.list.return_value.execute.return_value = (
        _empty_google_page
    )
    monkeypatch.setattr(
        google_calendar_adapter, "build", MagicMock(name="google_build", return_value=google_client)
    )
    mock_credentials = MagicMock(name="GoogleCredentials")
    mock_credentials.return_value.valid = True
    monkeypatch.setattr(google_calendar_adapter, "Credentials", mock_credentials)
    monkeypatch.setattr(google_calendar_adapter, "Request", MagicMock(name="google_Request"))

    # Microsoft: the Graph API client is fully mocked (no test_connection / Graph calls).
    # Paginated reads return empty so the adapter's pagination loops terminate.
    ms_client = MagicMock(name="ms_outlook_client")
    ms_client.test_connection.return_value = True
    ms_client.list_events.return_value = []
    ms_client.list_calendars.return_value = []
    ms_client.get_room_events.return_value = []
    ms_client.get_events_delta.return_value = {"events": [], "next_link": None, "delta_link": None}
    ms_client.get_room_events_delta.return_value = {
        "events": [],
        "next_link": None,
        "delta_link": None,
    }
    monkeypatch.setattr(
        ms_outlook_calendar_adapter,
        "MSOutlookCalendarAPIClient",
        MagicMock(name="MSOutlookCalendarAPIClient", return_value=ms_client),
    )


def _reseed_default_billing_plan():
    """Recreate the ``unlimited`` plan that migration ``0007_seed_billing_plans`` seeds.

    ``@pytest.mark.django_db(transaction=True)`` tests run against a real
    ``TransactionTestCase``, which *flushes* every table afterwards and (without
    ``serialized_rollback``) does not restore data created by data migrations. So the
    seeded plan catalog disappears for every test that runs after the first transactional
    one, and any organization created from then on has no default plan to land on.

    Keep the shape in sync with ``payments/migrations/0007_seed_billing_plans.py``: a
    ``PlanLimit`` row for **every** ``LimitedResource`` (``assert_plan_is_complete``
    refuses a plan that omits one) with a NULL ceiling, and every ``Entitlement`` granted.
    """
    from payments.billing_constants import Entitlement, LimitedResource, LimitKind
    from payments.models import BillingPlan, PlanEntitlement, PlanLimit

    postpaid = {LimitedResource.EVENT_OCCURRENCES}
    plan, _ = BillingPlan.objects.update_or_create(
        slug="unlimited",
        defaults={
            "name": "Unlimited",
            "is_active": True,
            "is_default_for_new_organizations": True,
            "monthly_price": 0,
            "annual_price": None,
            "currency": "USD",
            "grace_period_days": None,
        },
    )
    for resource_key in LimitedResource.values:
        PlanLimit.objects.update_or_create(
            plan=plan,
            resource_key=resource_key,
            defaults={
                "limit_value": None,
                "kind": (LimitKind.POSTPAID if resource_key in postpaid else LimitKind.PREPAID),
                "overage_unit_price": None,
            },
        )
    for entitlement_key in Entitlement.values:
        PlanEntitlement.objects.update_or_create(
            plan=plan, entitlement_key=entitlement_key, defaults={"is_enabled": True}
        )
    return plan


@pytest.fixture(autouse=True)
def provision_default_subscription(request):
    """Give every ``Organization`` created during a test the ``Subscription`` production
    would have given it.

    Production organizations are created through ``OrganizationService``, which calls
    ``SubscriptionService.create_subscription_for_organization`` — so the "no
    plan-less state" rule holds for every billing root, and every one of them lands
    on the seeded ``unlimited`` plan (``is_default_for_new_organizations=True``), whose
    ``_sync_entitlements`` writes every entitlement enabled. Tests that build an
    ``Organization`` with ``baker.make`` bypass that service and produce an organization
    in a state production cannot reach: no ``Subscription`` at all.

    ``EntitlementService.has_entitlement`` fails **closed** on that state, deliberately
    (see its docstring): for a boolean gate, "we don't know" resolving to *granted* would
    hand paid features to exactly the organizations whose billing state is corrupt. So the
    plan-less fixture — not the production semantics — is what has to change, and this is
    that change applied once instead of in every fixture in the suite.

    Reseller children are skipped by ``create_subscription_for_organization`` itself (they
    pool against their billing root), so this stays consistent with ``resolve_billing_root``.

    Opt out with ``@pytest.mark.no_auto_subscription`` when a test builds its own
    ``Subscription`` — ``Subscription.organization`` is a ``OneToOneField``, so a second
    row raises ``IntegrityError`` — or when it deliberately exercises the plan-less state.
    """
    if request.node.get_closest_marker("no_auto_subscription"):
        yield
        return

    from django.db.models.signals import post_save

    from organizations.models import Organization

    def _provision(sender, instance, created, raw=False, **kwargs):
        if not created or raw:
            return
        # Deferred import: `di_core.containers.container` is only *assigned* in
        # `DICoreConfig.ready()` -- a module-level import binds `None` forever. The
        # container-built service is what production and the rest of the suite's
        # tests use (see e.g. `public_api/tests/test_system_user_limits.py`'s
        # `service` fixture and `payments/tests/test_prepaid_resource_coverage.py`'s
        # `_container()` helper) rather than a hand-constructed one.
        from di_core.containers import container
        from payments.exceptions import NoDefaultBillingPlanError

        assert container is not None, "DI container is only assigned in DICoreConfig.ready()"
        subscription_service = container.subscription_service()
        try:
            subscription_service.create_subscription_for_organization(instance)
        except NoDefaultBillingPlanError:
            _reseed_default_billing_plan()
            subscription_service.create_subscription_for_organization(instance)

    post_save.connect(
        _provision, sender=Organization, dispatch_uid="conftest_provision_default_subscription"
    )
    try:
        yield
    finally:
        post_save.disconnect(
            sender=Organization, dispatch_uid="conftest_provision_default_subscription"
        )


@pytest.fixture
def user_password():
    from users.factories import DEFAULT_TEST_USER_PASSWORD

    return DEFAULT_TEST_USER_PASSWORD


@pytest.fixture
def user(user_password):
    from users.factories import UserFactory

    return UserFactory().create_user()


@pytest.fixture
def auth_client(user, user_password):
    client = APIClient()
    client.login(email=user.email, password=user_password)
    return client


@pytest.fixture
def anonymous_client():
    client = APIClient()
    return client


@pytest.fixture
def di_container():
    """Fixture to create a DI container."""
    from di_core.containers import container

    return container


@pytest.fixture
def assert_no_unbound_scoped_queries():
    """Tripwire: fail the test if a query on an organization-scoped table runs
    with no organization bound via ``common.organization_context`` **and**
    without naming one itself.

    Phase 0 of the vinta-django-orgs migration
    (``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``)
    threaded an explicit ``organization_context(...)`` binding through every
    Celery task and management command that touches a scoped model, and this
    fixture asked "did anything run unbound?" -- because an unbound call site was
    what Phase 2 would break.

    Phase 2a landed that flip for ``calendar_integration``: ``objects`` scopes to
    the bound organization and, under ``STRICT_ORGANIZATION_FILTER``, raises when
    nothing is bound. The manager now enforces the unbound half itself, loudly.
    What it cannot enforce is the deliberate escape hatch -- ``unscoped()`` /
    ``original_manager`` bypass the context on purpose and name no organization --
    so that is what this reports. ``filter_by_organization(...)`` is not a
    violation: it says which organization it means.

    Deliberately **opt-in**, not autouse. Requests do not bind an organization
    until Phase 2b's ``TenantScopedViewMixin`` change, and plenty of tests read
    through ``original_manager`` on purpose; autouse would fail those for being
    explicit about crossing organizations rather than for leaking across them.

    Implementation lives in ``common.organization_context_test_support`` (kept
    independent of pytest's fixture protocol so it is unit-testable on its own
    -- see that module's tests).
    """
    # Deferred: pytest imports this root ``conftest.py`` (module scope, see
    # its top-of-file imports above) during collection, before pytest-django
    # calls ``django.setup()`` -- so anything that touches the Django ORM
    # must wait until a fixture actually runs, not sit at module scope here.
    from common.organization_context_test_support import (
        assert_all_scoped_queries_are_bound,
        raise_if_unbound_scoped_queries_occurred,
    )

    with assert_all_scoped_queries_are_bound() as unbound_calls:
        yield unbound_calls

    raise_if_unbound_scoped_queries_occurred(unbound_calls)
