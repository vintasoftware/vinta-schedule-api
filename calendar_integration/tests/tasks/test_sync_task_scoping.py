"""What the Celery task organization binding does, before and after Phase 2a.

Phase 0 added ``common.organization_context.organization_context(...)`` around
this task body and proved it changed *nothing* observable -- the managers of the
day ignored the binding entirely and required their own explicit ``organization``
filter. **Phase 2a ends that**: ``calendar_integration``'s models now scope to the
bound organization implicitly, and with ``STRICT_ORGANIZATION_FILTER = True`` a
scoped read with nothing bound raises ``OrganizationNotFoundError`` instead of
silently returning nothing.

So the comparison this suite used to make -- run the body bound and unbound, and
assert the two are indistinguishable -- is no longer the contract, and asserting
it would assert the opposite of what the migration is for. The structure is kept
(same two runs, same normalized description of what happened) with the expectation
inverted: the *bound* run does the work, and the *unbound* run, which reproduces
the pre-Phase-0 code path exactly, now fails loudly at the first scoped read. That
is the whole safety argument for migrating without a feature flag, and it is
pinned here rather than asserted in prose.
"""

from __future__ import annotations

import contextlib
import datetime
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from vinta_orgs.exceptions import OrganizationNotFoundError

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import Calendar, CalendarSync, CalendarSyncStatus
from calendar_integration.tasks import calendar_sync_tasks
from common.organization_context import get_current_organization, organization_context
from organizations.models import Organization
from users.models import User


pytestmark = pytest.mark.django_db


@contextlib.contextmanager
def _unbound_organization_context(organization: Organization | None) -> Iterator[None]:
    """Stand-in for ``organization_context`` that binds nothing at all.

    Reproduces the pre-Phase-0 code path exactly: the task body runs
    unmodified, but no organization is ever bound to
    ``common.organization_context``. ``organization`` is accepted (matching
    the real context manager's signature) and deliberately unused.
    """
    del organization
    yield


def _make_org_and_account(name: str) -> tuple[Organization, Calendar, SocialAccount]:
    organization = Organization.objects.create(name=name)
    user = User.objects.create_user(email=f"{name.lower()}@example.com", password="testpass123")
    social_account = SocialAccount.objects.create(
        user=user, provider=CalendarProvider.GOOGLE, uid=f"uid-{name}"
    )
    SocialToken.objects.create(
        account=social_account,
        token="test_access_token",
        token_secret="test_refresh_token",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )
    calendar = Calendar.objects.create(
        name=f"{name} Calendar",
        description="A test calendar",
        external_id=f"cal-{name}",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )
    return organization, calendar, social_account


def _run_sync_calendar_task(
    organization: Organization,
    calendar: Calendar,
    social_account: SocialAccount,
    *,
    bind: bool,
) -> dict[str, Any]:
    """Run ``sync_calendar_task`` once and return a normalized description of
    what happened, so two runs (bound / unbound) can be compared without
    caring about pk-level identity (each run gets its own rows).
    """
    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=organization,
    )

    observed_bound_organization_id: int | None = None

    def _mock_sync_events(sync: CalendarSync) -> None:
        nonlocal observed_bound_organization_id
        bound = get_current_organization()
        observed_bound_organization_id = None if bound is None else bound.id
        sync.status = CalendarSyncStatus.SUCCESS
        sync.save()

    mock_service = MagicMock()
    mock_service.sync_events.side_effect = _mock_sync_events

    context_impl = organization_context if bind else _unbound_organization_context
    with patch.object(calendar_sync_tasks, "organization_context", context_impl):
        assert get_current_organization() is None
        result = calendar_sync_tasks.sync_calendar_task(
            "social_account",
            social_account.id,
            calendar_sync.id,
            organization.id,
            calendar_service=mock_service,
        )
        # The binding never survives the task, bound or not.
        assert get_current_organization() is None

    calendar_sync.refresh_from_db()

    return {
        "task_return_value": result,
        "final_status": calendar_sync.status,
        "authenticate_call_count": mock_service.authenticate.call_count,
        "authenticate_account": mock_service.authenticate.call_args.kwargs.get("account")
        if mock_service.authenticate.call_args
        else None,
        "sync_events_call_count": mock_service.sync_events.call_count,
        "observed_bound_organization_id": observed_bound_organization_id,
    }


class TestSyncCalendarTaskOrganizationBindingIsLoadBearing:
    def test_the_bound_run_works_and_the_unbound_run_raises(self):
        # Deliberately does NOT request ``assert_no_unbound_scoped_queries``:
        # the second run below is *supposed* to be unbound, so the tripwire would
        # fire by design rather than on a real defect.
        bound_org, bound_calendar, bound_account = _make_org_and_account("BoundOrg")
        unbound_org, unbound_calendar, unbound_account = _make_org_and_account("UnboundOrg")

        bound_result = _run_sync_calendar_task(bound_org, bound_calendar, bound_account, bind=True)

        # The binding happened, and the task did its work through it.
        assert bound_result["observed_bound_organization_id"] == bound_org.id
        assert bound_result == {
            **bound_result,
            "task_return_value": None,
            "final_status": CalendarSyncStatus.SUCCESS,
            "authenticate_call_count": 1,
            "sync_events_call_count": 1,
        }

        # The same body with nothing bound -- the pre-Phase-0 code path -- no
        # longer reads as "no data"; it refuses, at the first scoped read
        # (``CalendarSync.objects.get_not_started_calendar_sync(...)``).
        with pytest.raises(OrganizationNotFoundError):
            _run_sync_calendar_task(unbound_org, unbound_calendar, unbound_account, bind=False)

    def test_missing_organization_short_circuits_identically_bound_or_unbound(
        self, assert_no_unbound_scoped_queries
    ):
        """The early ``if not organization: return`` guard fires before the
        binding is ever entered, for both implementations -- a task
        dispatched with a stale/deleted organization id is a no-op either
        way, which this pins so a future change to the guard's placement
        cannot silently start binding an organization that does not exist.
        """
        for bind in (True, False):
            context_impl = organization_context if bind else _unbound_organization_context
            with patch.object(calendar_sync_tasks, "organization_context", context_impl):
                mock_service = MagicMock()
                result = calendar_sync_tasks.sync_calendar_task(
                    "social_account",
                    1,
                    1,
                    999_999,
                    calendar_service=mock_service,
                )
                assert result is None
                mock_service.authenticate.assert_not_called()
                mock_service.sync_events.assert_not_called()
                assert get_current_organization() is None
