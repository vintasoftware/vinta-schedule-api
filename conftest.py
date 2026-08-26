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


@pytest.fixture(scope="session", autouse=True)
def preload_urlconf():
    """Import the root URLconf once, in *setup*, instead of inside a test body.

    ``vinta_schedule_api/urls.py`` is not imported by ``django.setup()`` and not by
    collection -- the first ``reverse()`` or test-client request in a worker process
    triggers it. That import is not small: it pulls in every app's routes (viewsets,
    serializers, virtual models), the admin, drf-spectacular, and
    ``from public_api.schema import schema``, which builds the entire Strawberry GraphQL
    schema. Measured at 1.5s cold in a single process and 3.4s with the box under load,
    both without ``--cov``, which ``addopts`` adds.

    All of that used to be charged to the *call* phase of whichever test reversed a URL
    first, because ``timeout_func_only`` (see ``pytest.ini``) excludes fixtures and the
    database build but not a lazy import inside the test function. Under ``-n auto``
    every worker pays it, and they collide -- they walk the same early part of the
    collection order. The systematic victim was
    ``accounts/tests/test_views.py::TestProviderCallbackAPIView::test_missing_provider_id``,
    an assertion about a 400 response that has nothing to do with any of this.

    Session-scoped fixtures run in the setup phase, which the timeout already excludes,
    so this puts the cost where the rest of the one-time warm-up already lives.

    Deferred import: this module is imported during collection, before pytest-django
    calls ``django.setup()``.
    """
    from django.urls import get_resolver

    # Resolving the property is what triggers the import; the value itself is not needed.
    _ = get_resolver().url_patterns


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


def _reseed_billing_plans():
    """Recreate the ``unlimited`` and ``free`` plans that ``0007_seed_billing_plans``
    seeds, from the live catalog.

    ``@pytest.mark.django_db(transaction=True)`` tests run against a real
    ``TransactionTestCase``, which *flushes* every table afterwards and (without
    ``serialized_rollback``) does not restore data created by data migrations. So the
    seeded plan catalog disappears for every test that runs after the first transactional
    one, and any organization created from then on has no default plan to land on.

    Deferred import for the reason spelled out on ``assert_no_unbound_scoped_queries``
    below: pytest imports this module during collection, before pytest-django calls
    ``django.setup()``, so nothing that reaches the ORM may sit at module scope here.
    """
    from payments.tests.billing_fixtures import reseed_billing_plans

    reseed_billing_plans()


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
        from vinta_billing.exceptions import NoDefaultBillingPlanError

        from di_core.containers import container

        assert container is not None, "DI container is only assigned in DICoreConfig.ready()"
        subscription_service = container.subscription_service()
        try:
            subscription_service.create_subscription_for_organization(instance)
        except NoDefaultBillingPlanError:
            _reseed_billing_plans()
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


#: The pytest-django entry points that flush every table on teardown. A test naming any
#: of them -- or carrying ``@pytest.mark.django_db(transaction=True)``, or subclassing
#: ``TransactionTestCase`` -- destroys the rows the data migrations wrote for the rest
#: of the worker's session.
_FLUSHING_FIXTURES = frozenset({"transactional_db", "django_db_reset_sequences", "live_server"})

#: Everything pytest-django will give a test a database through. Read to decide whether
#: this fixture may touch the ORM at all -- see ``ensure_seeded_billing_catalog``.
_DATABASE_FIXTURES = _FLUSHING_FIXTURES | {"db", "django_db_serialized_rollback"}

#: Session state: has the plan catalog been flushed since it was last seeded? Module
#: scope rather than a fixture because it must outlive the test that set it -- it is
#: consumed by the *next* test, in the next fixture instance.
#:
#:
#: Covers flushes that happen *within* a session only. It starts ``False`` because at
#: session start nothing has flushed yet -- which is true of the process, and says
#: nothing about the database the process was handed. A database left flushed by a
#: *previous* ``--reuse-db`` run is the other half of the problem, and
#: ``_repaired_inherited_billing_catalog`` below is what answers it.
_billing_catalog_was_flushed = False

#: Did ``_repaired_inherited_billing_catalog`` have to rebuild the catalog from live
#: code because this session inherited a flushed database? Read by
#: ``ensure_seeded_billing_catalog`` to skip the tests that assert on the *migration's*
#: output, which a rebuild cannot stand in for.
_billing_catalog_was_rebuilt_from_live_code = False


def _node_uses_the_database(node) -> bool:
    """Same question as ``_test_uses_the_database``, asked of a collected item.

    Split out so ``_repaired_inherited_billing_catalog`` can ask it at *session* scope,
    where there is no ``request.node`` and no ``request.fixturenames`` -- only the
    collected items. ``node.fixturenames`` is populated during collection for function
    items, so the fixture-name half of the test still works here.
    """
    if node.get_closest_marker("django_db") is not None:
        return True
    if _DATABASE_FIXTURES & set(getattr(node, "fixturenames", ())):
        return True

    from django.test import TransactionTestCase

    cls = getattr(node, "cls", None)
    return isinstance(cls, type) and issubclass(cls, TransactionTestCase)


def _test_uses_the_database(request) -> bool:
    if _node_uses_the_database(request.node):
        return True
    return bool(_DATABASE_FIXTURES & set(request.fixturenames))


def _test_flushes_the_database(request) -> bool:
    marker = request.node.get_closest_marker("django_db")
    if marker is not None and marker.kwargs.get("transaction"):
        return True
    if _FLUSHING_FIXTURES & set(request.fixturenames):
        return True

    from django.test import TestCase, TransactionTestCase

    cls = getattr(request.node, "cls", None)
    return (
        isinstance(cls, type)
        and issubclass(cls, TransactionTestCase)
        and not issubclass(cls, TestCase)
    )


def _seeded_billing_catalog_is_missing() -> bool:
    """Is the seeded plan catalog absent from the database right now?

    The default plan is what every consumer of the catalog actually needs --
    ``SubscriptionService.create_subscription_for_organization`` raises
    ``NoDefaultBillingPlanError`` without it -- so its absence is the condition worth
    repairing, and one indexed existence check is cheap enough to run once per session.

    Deferred import for the reason given on ``assert_no_unbound_scoped_queries``: this
    module is imported during collection, before ``django.setup()``.
    """
    from vinta_billing.models import BillingPlan

    return not BillingPlan.objects.filter(is_default_for_new_organizations=True).exists()


@pytest.fixture(scope="session", autouse=True)
def _repaired_inherited_billing_catalog(request):
    """Put the catalog back once per session when the database arrived without one.

    Session-scoped **and autouse**, which is what puts it before every function-scoped
    fixture in the run. Both matter: pytest-django wraps each non-transactional test in
    an atomic block and rolls it back, so a reseed that lands inside one is undone at
    that test's teardown. Requesting this lazily from ``ensure_seeded_billing_catalog``
    is not enough to avoid that -- ``vinta_orgs.testing.seeded_organization_groups`` is a
    *plugin* autouse fixture and pytest sets plugin fixtures up before conftest ones, so
    it has already forced ``_django_db_helper`` (and with it the test transaction) by the
    time anything in this file runs. Only session scope is reliably earlier.

    It stays lazy about the database anyway: a run whose collected items include no
    database test must not build a test database just to inspect a catalog nothing will
    read, and must keep pytest-django's ``Database access not allowed`` guard firing.
    Hence the scan of ``session.items`` before ``django_db_setup`` is pulled up.

    This handles the *inherited* case only: a database left flushed by a previous
    ``--reuse-db`` run. Flushes that happen *during* a session are handled by
    ``ensure_seeded_billing_catalog``'s per-test flag, which is a different problem with
    a different answer (those tests are transactional, so their repair commits anyway).

    Repairing here does put a catalog *rebuilt from live code* under the whole session,
    which is precisely what ``no_billing_catalog_reseed`` exists to prevent for the
    modules whose subject is what ``0007`` actually wrote. Those cannot be served by any
    repair -- what they need is a database the migration really ran against. So this
    records that it rebuilt, and ``ensure_seeded_billing_catalog`` *skips* those tests
    with an actionable reason rather than letting them report green on a synthetic
    catalog. The contract in ``payments/tests/test_plan_seed_migration.py``'s docstring
    holds: it still never goes green on a rebuilt catalog. It just says "rerun with
    ``--create-db``" instead of failing with a bare ``BillingPlan.DoesNotExist``.
    """
    global _billing_catalog_was_rebuilt_from_live_code

    if not any(_node_uses_the_database(item) for item in request.session.items):
        yield
        return

    request.getfixturevalue("django_db_setup")
    with request.getfixturevalue("django_db_blocker").unblock():
        if _seeded_billing_catalog_is_missing():
            _reseed_billing_plans()
            _billing_catalog_was_rebuilt_from_live_code = True
    yield


@pytest.fixture(autouse=True)
def ensure_seeded_billing_catalog(request):
    """Put the seeded plan catalog back, but **only after something destroyed it**.

    Two rules, and the reasons they are rules rather than "reseed before every test",
    which is what this fixture used to do:

    1. **It does not request ``db``.** A root-level autouse fixture that requests ``db``
       hands database access to every test in the repository, and pytest-django's
       ``RuntimeError: Database access not allowed`` -- the thing that catches a test
       which forgot ``@pytest.mark.django_db`` -- stops firing repo-wide. So the marker
       and the fixture names are inspected first, and ``_django_db_helper`` is forced up
       by name only once a database is known to be wanted. Same shape as
       ``vinta_orgs.testing.seeded_organization_groups``, for the same reason.
    2. **It only runs after a flush.** The catalog is written by
       ``payments/migrations/0007_seed_billing_plans.py`` and survives in the test
       database until a ``transaction=True`` test flushes it; pytest-django sorts every
       transactional test *after* the non-transactional ones, so the damage is confined
       to transactional tests that run later. Reseeding unconditionally would recreate,
       from live code, exactly what ``0007`` seeds -- which makes every assertion about
       the migration's output pass whether or not the migration still does anything.
       ``payments/tests/test_plan_seed_migration.py`` is precisely that module, and it
       went green with ``0007``'s ``RunPython`` gutted while this fixture was
       unconditional.

    ``@pytest.mark.no_billing_catalog_reseed`` opts out of the repair entirely, for the
    tests whose subject *is* what the migration left behind. They would rather fail than
    be handed a synthetic catalog.
    """
    global _billing_catalog_was_flushed

    if not _test_uses_the_database(request):
        yield
        return

    if _billing_catalog_was_rebuilt_from_live_code and request.node.get_closest_marker(
        "no_billing_catalog_reseed"
    ):
        pytest.skip(
            "This test asserts on what payments/migrations/0007_seed_billing_plans.py "
            "wrote, but the reused test database arrived without a seeded catalog (a "
            "previous run's last transactional test flushed it), so it was rebuilt from "
            "payments.billing_plans_catalog. Passing against that rebuild would prove "
            "nothing about the migration. Rerun with --create-db (or `make test_reset`)."
        )

    if _billing_catalog_was_flushed and not request.node.get_closest_marker(
        "no_billing_catalog_reseed"
    ):
        # Force the database up first: an autouse fixture runs before the ``db`` /
        # ``transactional_db`` fixture a test requests by name, so without this the
        # reseed would hit pytest-django's access blocker.
        request.getfixturevalue("_django_db_helper")
        _reseed_billing_plans()
        _billing_catalog_was_flushed = False

    yield

    if _test_flushes_the_database(request):
        # Set at *our* teardown, which is earlier than the flush itself -- that runs in
        # pytest-django's own finalizer, later than any conftest fixture can reach. The
        # flag is read at the next test's setup, which is after it either way.
        _billing_catalog_was_flushed = True


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

    An explicit ``organization_context(...)`` binding is threaded through every
    Celery task and management command that touches a scoped model, and this
    fixture asks "did anything run unbound?" -- because an unbound call site is
    what breaks once a manager enforces scoping.

    For ``calendar_integration``, ``objects`` scopes to
    the bound organization and, under ``STRICT_ORGANIZATION_FILTER``, raises when
    nothing is bound. The manager enforces the unbound half itself, loudly.
    What it cannot enforce is the deliberate escape hatch -- ``unscoped()`` /
    ``original_manager`` bypass the context on purpose and name no organization --
    so that is what this reports. ``filter_by_organization(...)`` is not a
    violation: it says which organization it means.

    Deliberately **opt-in**, not autouse. Requests do not bind an organization
    via ``organization_context``, and plenty of tests read
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
