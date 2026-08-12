"""Integration tests: metering against the *real* recurrence machinery.

The unit tests build series with the factory. These drive
``CalendarEventService`` — the code an actual user's edit runs through — because
the whole risk lives in what that machinery does to event *identity* when a series
is edited. A recurrence exception and a bulk-modification continuation are both
separate ``CalendarEvent`` rows that represent occurrences of an existing series;
counting either one alongside the series it came from is a double charge that the
unique constraint cannot catch, because the two rows have genuinely different ids.

``reconcile_period`` reporting zero drift over a closed cycle is the acceptance
criterion and the safeguard against silent revenue drift, so it is asserted after
every scenario here rather than only in its own test.
"""

import datetime
from unittest.mock import Mock, patch

from django.test import override_settings

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken

from calendar_integration.constants import CalendarProvider, CalendarType, RecurrenceFrequency
from calendar_integration.factories import CalendarEventFactory
from calendar_integration.models import (
    AvailableTime,
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
)
from calendar_integration.services.calendar_event_service import CalendarEventService
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
)
from calendar_integration.services.calendar_service import CalendarService
from payments.models import MeteredOccurrence, Subscription
from payments.services.metering_service import MeteringService
from tenancy.models import Organization, OrganizationMembership
from users.models import Profile, User


PERIOD_START = datetime.datetime(2025, 6, 1, 0, 0, tzinfo=datetime.UTC)
PERIOD_END = datetime.datetime(2025, 7, 1, 0, 0, tzinfo=datetime.UTC)
FIRST_MONDAY = datetime.datetime(2025, 6, 2, 10, 0, tzinfo=datetime.UTC)

#: Mondays in June 2025.
ALL_MONDAYS = [FIRST_MONDAY + datetime.timedelta(weeks=week) for week in range(5)]

# `GoogleCalendarAdapter.__init__` raises `ImproperlyConfigured` without these.
# Locally they arrive from `.env`; CI leaves them empty, so a test that
# authenticates a Google-provider calendar passes on a developer machine and fails
# in CI unless they are supplied explicitly.
_with_google_credentials = override_settings(
    GOOGLE_CLIENT_ID="test-google-client-id",
    GOOGLE_CLIENT_SECRET="test-google-client-secret",  # noqa: S106 - dummy value, not a credential
)


@pytest.fixture
def mock_google_adapter():
    with patch(
        "calendar_integration.services.calendar_adapters.google_calendar_adapter.GoogleCalendarAdapter"
    ) as mock_adapter_class:
        mock_adapter = Mock()
        mock_adapter.provider = CalendarProvider.GOOGLE
        # Avoid Django expression-resolution attribute hits on the Mock.
        del mock_adapter.resolve_expression
        del mock_adapter.get_source_expressions
        mock_adapter_class.return_value = mock_adapter
        mock_adapter_class.from_service_account_credentials.return_value = mock_adapter
        yield mock_adapter


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Reconciliation Org", should_sync_rooms=False)


@pytest.fixture
def subscription(organization: Organization) -> Subscription:
    subscription = Subscription.objects.get(organization=organization)
    subscription.current_period_start = PERIOD_START
    subscription.current_period_end = PERIOD_END
    subscription.save(update_fields=["current_period_start", "current_period_end", "modified"])
    return subscription


@pytest.fixture
def social_account(db) -> SocialAccount:
    user = User.objects.create_user(email="reconciliation@example.com", password="testpass123")
    Profile.objects.create(user=user)
    return SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="77777")


@pytest.fixture
def social_token(social_account: SocialAccount) -> SocialToken:
    return SocialToken.objects.create(
        account=social_account,
        token="test_access_token",
        token_secret="test_refresh_token",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    """A virtual calendar that manages its own availability, plus one open window.

    Two deliberate choices, neither of which affects what is under test:

    - ``manage_available_windows=True`` plus a wide window, because ``create_event``
      refuses to write outside one (``NoAvailableTimeWindowsError``) and the
      recurrence-exception path goes through ``create_event``.
    - ``calendar_type=VIRTUAL``, so event writes stay internal instead of being
      pushed to the (mocked) Google adapter. Metering reads ``CalendarEvent`` rows;
      how they reached the provider is irrelevant, and a half-configured mock
      adapter would only add a failure mode unrelated to billing.
    """
    calendar = Calendar.objects.create(
        name="Reconciliation Calendar",
        description="",
        external_id="reconciliation_cal_1",
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.VIRTUAL,
        organization=organization,
        manage_available_windows=True,
    )
    AvailableTime.objects.create(
        calendar_fk=calendar,
        organization=organization,
        start_time_tz_unaware=PERIOD_START - datetime.timedelta(days=30),
        end_time_tz_unaware=PERIOD_END + datetime.timedelta(days=30),
        timezone="UTC",
    )
    return calendar


@pytest.fixture
def calendar_management_token(
    calendar: Calendar, social_account: SocialAccount
) -> CalendarManagementToken:
    """Owner-level calendar token, so the writes these tests drive are authorized.

    Mirrors ``calendar_integration/tests/services/test_calendar_event_service.py``:
    the exception and bulk-modification paths go through ``create_event`` /
    ``update_event``, which check calendar permissions.
    """
    OrganizationMembership.objects.get_or_create(
        user=social_account.user, organization=calendar.organization
    )
    token = CalendarManagementToken.objects.create(
        calendar=calendar,
        membership_user_id=social_account.user.id,
        token_hash="reconciliation_token_hash",
        organization=calendar.organization,
    )
    token.permissions.all().delete()
    for permission_str in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        token.permissions.create(
            permission=permission_str,
            organization_id=calendar.organization_id,
        )
    return token


@pytest.fixture
def event_service(
    social_account: SocialAccount,
    social_token: SocialToken,
    mock_google_adapter,
    calendar: Calendar,
    calendar_management_token: CalendarManagementToken,
) -> CalendarEventService:
    """The real event service, wired exactly as the facade wires it internally."""
    with _with_google_credentials:
        facade = CalendarService()
        facade.authenticate(account=social_account.user, organization=calendar.organization)
    assert facade._context is not None, "authenticate() must have built the shared context"
    return CalendarEventService(
        context=facade._context,
        recurrence_manager=facade._recurrence_manager,
        calendar_cache=facade._calendar_cache,
        host=facade,
    )


@pytest.fixture
def metering_service() -> MeteringService:
    from di_core.containers import container

    assert container is not None
    return container.metering_service()


def _grant_event_owner_token(event: CalendarEvent, social_account: SocialAccount) -> None:
    """Owner-level token on a specific event.

    ``create_recurring_event_bulk_modification`` truncates the parent series through
    ``update_event``, whose permission check looks for an *event*-scoped token, not
    the calendar-scoped one.
    """
    token = CalendarManagementToken.objects.create(
        event_fk=event,
        membership_user_id=social_account.user.id,
        token_hash=f"reconciliation_event_token_{event.pk}",
        organization=event.organization,
    )
    token.permissions.all().delete()
    for permission_str in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        token.permissions.create(
            permission=permission_str,
            organization_id=event.organization_id,
        )


@pytest.fixture
def weekly_series(calendar: Calendar) -> CalendarEvent:
    """Five Mondays in June 2025, bounded so a split leaves a checkable total."""
    return CalendarEventFactory.create_recurring_event(
        calendar=calendar,
        title="Weekly Sync",
        description="",
        start_time=FIRST_MONDAY,
        end_time=FIRST_MONDAY + datetime.timedelta(hours=1),
        frequency=RecurrenceFrequency.WEEKLY,
        count=5,
        by_weekday="MO",
        external_id="weekly_master_reconciliation",
    )


def _occurrence_starts(subscription: Subscription) -> list[datetime.datetime]:
    return sorted(
        MeteredOccurrence.objects.filter(subscription=subscription).values_list(
            "occurrence_start", flat=True
        )
    )


def _meter_the_period(metering_service: MeteringService, subscription: Subscription) -> None:
    metering_service.meter_occurrences_for_period(subscription, PERIOD_START, PERIOD_END)


@pytest.mark.django_db
class TestReconciliation:
    def test_a_fully_metered_period_reports_zero_drift(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """Acceptance: ``reconcile_period`` reports zero drift for a closed period."""
        _meter_the_period(metering_service, subscription)

        report = metering_service.reconcile_period(subscription, PERIOD_START)

        assert report.expected_count == 5
        assert report.metered_count == 5
        assert report.unmetered == ()
        assert report.orphaned == ()
        assert report.drift == 0
        assert report.is_clean

    def test_reconciliation_resolves_the_period_from_any_moment_inside_it(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        _meter_the_period(metering_service, subscription)

        report = metering_service.reconcile_period(
            subscription, datetime.datetime(2025, 6, 17, 3, 14, tzinfo=datetime.UTC)
        )

        assert (report.billing_period_start, report.billing_period_end) == (
            PERIOD_START,
            PERIOD_END,
        )
        assert report.drift == 0

    def test_a_sweep_that_never_ran_is_reported_as_unmetered_drift(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """The under-billing direction: usage happened and was never recorded."""
        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, ALL_MONDAYS[2])

        report = metering_service.reconcile_period(subscription, PERIOD_START)

        assert report.metered_count == 2
        assert report.expected_count == 5
        assert [identity.occurrence_start for identity in report.unmetered] == ALL_MONDAYS[2:]
        assert report.orphaned == ()
        assert report.drift == 3

    def test_a_recorded_occurrence_the_period_no_longer_expands_to_is_reported_as_orphaned(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        organization: Organization,
        weekly_series: CalendarEvent,
    ):
        """The over-billing direction — and the deliberately ambiguous one.

        Deleting an event leaves its metered rows behind on purpose (``event_id`` is
        a soft reference), because an occurrence that was billed stays billed. So a
        non-zero ``orphaned`` is a prompt to look, not proof of a defect, which is
        why reconciliation reports the two directions separately instead of netting
        them into a single number.
        """
        _meter_the_period(metering_service, subscription)
        weekly_series.delete()

        report = metering_service.reconcile_period(subscription, PERIOD_START)

        assert report.expected_count == 0
        assert report.metered_count == 5
        assert len(report.orphaned) == 5
        assert report.unmetered == ()

    def test_reconciliation_writes_nothing(
        self,
        metering_service: MeteringService,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """A reconciliation that repaired itself would hide the thing it repaired."""
        metering_service.meter_occurrences_for_period(subscription, PERIOD_START, ALL_MONDAYS[1])
        before = set(
            MeteredOccurrence.objects.values_list("event_id", "occurrence_start", "unit_price")
        )

        metering_service.reconcile_period(subscription, PERIOD_START)
        metering_service.reconcile_period(subscription, PERIOD_START)

        assert (
            set(MeteredOccurrence.objects.values_list("event_id", "occurrence_start", "unit_price"))
            == before
        )


@pytest.mark.django_db
class TestRecurrenceExceptionCountedOnce:
    """A modified occurrence is one occurrence, not two."""

    def test_a_modified_occurrence_is_metered_once(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """Editing the third Monday must not add a sixth billable occurrence.

        ``create_recurring_event_exception`` writes a **new** ``CalendarEvent`` row
        for the modified occurrence and records an ``EventRecurrenceException``
        pointing at it. Both the series and that row exist afterwards; only the
        expansion knows they are the same occurrence.
        """
        exception_event = event_service.create_recurring_event_exception(
            parent_event=weekly_series,
            exception_date=ALL_MONDAYS[2].date(),
            modified_title="Moved",
        )
        assert exception_event is not None
        assert CalendarEvent.objects.filter(organization=subscription.organization).count() == 2

        _meter_the_period(metering_service, subscription)

        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5
        assert metering_service.reconcile_period(subscription, PERIOD_START).drift == 0

    def test_the_modified_occurrence_stays_attributed_to_the_series_and_its_slot(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """The edited occurrence is *not* recorded under the new row's id.

        The exception is a real ``CalendarEvent`` with its own pk, and recording the
        occurrence under that pk is the obvious implementation — it is also the one
        that double-bills, because the same occurrence then has two identities
        depending on whether it has been edited yet. It is recorded under the series
        and the slot it replaced instead.
        """
        exception_event = event_service.create_recurring_event_exception(
            parent_event=weekly_series,
            exception_date=ALL_MONDAYS[2].date(),
            modified_title="Moved",
        )
        assert exception_event is not None
        assert exception_event.pk != weekly_series.pk

        _meter_the_period(metering_service, subscription)

        rows = MeteredOccurrence.objects.filter(subscription=subscription)
        assert set(rows.values_list("event_id", flat=True)) == {weekly_series.pk}
        assert not rows.filter(event_id=exception_event.pk).exists()
        assert _occurrence_starts(subscription) == ALL_MONDAYS

    def test_a_cancelled_occurrence_is_not_metered(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """A cancelled occurrence did not happen, so it is not billable."""
        event_service.create_recurring_event_exception(
            parent_event=weekly_series,
            exception_date=ALL_MONDAYS[3].date(),
            is_cancelled=True,
        )

        _meter_the_period(metering_service, subscription)

        assert _occurrence_starts(subscription) == [
            ALL_MONDAYS[0],
            ALL_MONDAYS[1],
            ALL_MONDAYS[2],
            ALL_MONDAYS[4],
        ]
        assert metering_service.reconcile_period(subscription, PERIOD_START).drift == 0

    def test_metering_before_the_edit_and_again_after_it_still_bills_five(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """The realistic sequence: the sweep runs, then a user edits an occurrence.

        This is the scenario the whole identity design exists for. The edit writes a
        new ``CalendarEvent`` row for that occurrence, so a meter keyed on the row
        would see something it had never seen before and record a sixth billable
        occurrence for a month that only had five. Keyed on the series and the slot,
        the second sweep's insert collides and does nothing.
        """
        _meter_the_period(metering_service, subscription)
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5

        event_service.create_recurring_event_exception(
            parent_event=weekly_series,
            exception_date=ALL_MONDAYS[2].date(),
            modified_title="Renamed only",
        )
        _meter_the_period(metering_service, subscription)

        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5


@pytest.mark.django_db
class TestBulkModificationContinuationCountedOnce:
    """A split series is one series for billing purposes."""

    def test_a_continuation_does_not_re_bill_the_occurrences_it_took_over(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        social_account: SocialAccount,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """Splitting the series from the third Monday leaves five occurrences, not eight.

        ``create_recurring_event_bulk_modification`` truncates the parent's rule and
        creates a continuation event carrying the rest. Both rows are recurring
        masters afterwards. They tile the month rather than overlapping it — which
        is only true because the meter expands with ``get_occurrences_in_range``
        and never follows ``bulk_modifications`` from the parent as well.
        """
        _grant_event_owner_token(weekly_series, social_account)
        continuation = event_service.create_recurring_event_bulk_modification(
            parent_event=weekly_series,
            modification_start_date=ALL_MONDAYS[2],
            modified_title="Second half",
        )
        assert continuation is not None

        _meter_the_period(metering_service, subscription)

        assert _occurrence_starts(subscription) == ALL_MONDAYS
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5
        assert metering_service.reconcile_period(subscription, PERIOD_START).drift == 0

    def test_the_split_moves_later_occurrences_onto_the_continuation_row(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        social_account: SocialAccount,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """Every occurrence stays attributed to the series root, split or not.

        The continuation is a separate ``CalendarEvent``; if occurrences after the
        split were recorded under *its* pk, a split applied to an already-metered
        month would re-bill the whole tail.
        """
        _grant_event_owner_token(weekly_series, social_account)
        continuation = event_service.create_recurring_event_bulk_modification(
            parent_event=weekly_series,
            modification_start_date=ALL_MONDAYS[2],
            modified_title="Second half",
        )
        assert continuation is not None

        _meter_the_period(metering_service, subscription)

        attribution = {
            occurrence_start: event_id
            for event_id, occurrence_start in MeteredOccurrence.objects.filter(
                subscription=subscription
            ).values_list("event_id", "occurrence_start")
        }
        assert continuation.pk != weekly_series.pk
        assert set(attribution) == set(ALL_MONDAYS)
        assert set(attribution.values()) == {weekly_series.pk}

    def test_a_title_only_split_is_the_vacuous_case(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        social_account: SocialAccount,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """Why the three tests above prove less than they appear to.

        Each passes ``modified_title`` only. A title change leaves every occurrence
        at the time it already had, so parent and continuation generate the *same
        instants*, and the identity tuple collapses the two into one row by
        coincidence rather than by design. The dedup being asserted is real but
        vacuous: it would hold under almost any identity scheme.

        The arguments that make ``create_recurring_event_bulk_modification`` worth
        having — ``modified_start_time_offset`` and ``modification_rrule_string`` —
        move occurrences, and are exercised in
        ``TestBulkModificationWithOffsetOverBills`` below, where the dedup does not
        hold. This test exists to stop a reader generalising from the easy case.
        """
        _grant_event_owner_token(weekly_series, social_account)
        event_service.create_recurring_event_bulk_modification(
            parent_event=weekly_series,
            modification_start_date=ALL_MONDAYS[2],
            modified_title="Renamed tail",
        )

        _meter_the_period(metering_service, subscription)

        # The continuation's occurrences land on instants the parent already
        # generated, so there is nothing for identity to distinguish.
        assert _occurrence_starts(subscription) == ALL_MONDAYS

    def test_a_split_after_metering_does_not_re_bill_the_first_half(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        social_account: SocialAccount,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """A split applied to a period that has already been swept.

        The occurrences before the split date keep the parent's id and their start
        times, so re-sweeping collides on the constraint. This is the case the
        overlapping sweep window makes routine.
        """
        _meter_the_period(metering_service, subscription)

        _grant_event_owner_token(weekly_series, social_account)
        event_service.create_recurring_event_bulk_modification(
            parent_event=weekly_series,
            modification_start_date=ALL_MONDAYS[3],
            modified_title="Tail",
        )
        _meter_the_period(metering_service, subscription)

        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5
        assert _occurrence_starts(subscription) == ALL_MONDAYS


@pytest.mark.django_db
class TestFirstOccurrenceSplitIsNotDeduplicated:
    """Characterization of a **known, unfixed** double-count.

    Editing or cancelling the *first* occurrence of a series does not create a
    recurrence exception like every other occurrence does. Instead
    ``RecurrenceManager.create_recurring_exception_generic`` strips the master's
    recurrence rule and creates an entirely **new** recurring event starting at the
    second occurrence — with no ``bulk_modification_parent``, no
    ``parent_recurring_object``, and no other link back to the series it replaced.

    Every other identity-churn path here is absorbed because the meter can
    find its way back to the series root. This one cannot: there is nothing to
    follow. Re-metering a stretch that has already been billed therefore records the
    surviving occurrences a second time, under the replacement series' pk, and the
    ledger's unique constraint cannot catch it because the rows genuinely differ.

    These tests assert the **current, wrong** behavior on purpose, so that the size
    of the defect is written down and so that whoever fixes the recurrence machinery
    (or gives occurrences a durable identity) is told by a failing test that this
    file needs revisiting. They are not an endorsement of the behavior.

    The same unlinked-replacement machinery is already recorded as an unbounded
    over-count in the availability-window usage counter (see
    ``AvailableTimeQuerySet.only_user_authored``). This is that defect surfacing a
    second time, in the place where it costs money.
    """

    def test_editing_the_first_occurrence_re_bills_the_rest_of_the_series(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        _meter_the_period(metering_service, subscription)
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5

        event_service.create_recurring_event_exception(
            parent_event=weekly_series,
            exception_date=ALL_MONDAYS[0].date(),
            modified_title="First one moved",
        )
        replacement = (
            CalendarEvent.objects.filter(organization=subscription.organization)
            .exclude(pk=weekly_series.pk)
            .filter(recurrence_rule__isnull=False)
            .first()
        )
        assert replacement is not None, (
            "the first-occurrence edit is expected to create a replacement series"
        )
        assert replacement.bulk_modification_parent_fk_id is None, (
            "if this replacement ever gains a link to its parent, the meter can "
            "resolve it to the series root and this whole test class should go away"
        )

        _meter_the_period(metering_service, subscription)

        # KNOWN DEFECT: the four surviving occurrences are billed a second time,
        # under the replacement series' pk. The correct total is 5.
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 9
        assert (
            MeteredOccurrence.objects.filter(
                subscription=subscription, event_id=replacement.pk
            ).count()
            == 4
        )

    def test_the_defect_needs_the_period_to_have_been_metered_before_the_edit(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """Editing first, then metering, is correct — which bounds the exposure.

        The double-count needs the *same* occurrences to be metered under both
        identities, so it can only affect a stretch of time that a sweep has already
        covered and that a later sweep covers again. In production that is the
        overlap between consecutive sweep windows, not the whole cycle.
        """
        event_service.create_recurring_event_exception(
            parent_event=weekly_series,
            exception_date=ALL_MONDAYS[0].date(),
            modified_title="First one moved",
        )

        _meter_the_period(metering_service, subscription)

        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5
        assert metering_service.reconcile_period(subscription, PERIOD_START).drift == 0

    def test_reconciliation_reports_the_double_count_as_orphaned_rows(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """The mitigation that is actually in place for this defect.

        Nothing prevents the double-count, but it is not silent: a reconciliation of
        the closed period reports the superseded rows as ``orphaned``, which is the
        signal finance acts on.
        """
        _meter_the_period(metering_service, subscription)
        event_service.create_recurring_event_exception(
            parent_event=weekly_series,
            exception_date=ALL_MONDAYS[0].date(),
            modified_title="First one moved",
        )
        _meter_the_period(metering_service, subscription)

        report = metering_service.reconcile_period(subscription, PERIOD_START)

        assert report.metered_count == 9
        assert report.expected_count == 5
        assert report.drift == 4
        assert len(report.orphaned) == 4
        assert report.unmetered == ()


@pytest.mark.django_db
class TestBulkModificationWithOffsetTilesTheTimeline:
    """A bulk modification with an offset must tile the timeline, not double it.

    ``create_recurring_event_bulk_modification`` with a
    ``modified_start_time_offset`` is an ordinary owner action: "from next Monday,
    the standup moves to 10:30". Applied to a five-occurrence weekly series it must
    still produce **five** billable occurrences — Monday 1 at 10:00 from the
    truncated parent, Mondays 2-5 at 10:30 from the continuation.

    This class used to assert eight (and nine over an already-metered month),
    characterizing a defect where the truncation never reached the database.
    ``RecurrenceRuleSplitter`` returned ``copy.deepcopy`` clones, which preserve the
    saved model's ``pk``, so both halves of a split aliased the original rule row;
    the continuation's ``save()`` then ``UPDATE``d the parent's row and erased the
    ``UNTIL`` just written. The splitter now returns genuinely detached copies and
    each side owns its own row, so the parent stops where it is told to.

    What remains, and is asserted below, is the **identity churn** the metering
    design was always built around: an occurrence that moves to a new time has a
    new identity, so a period metered *before* the edit keeps rows for the old
    slots. Unlike the truncation defect, this needs an already-metered window and
    ``reconcile_period`` surfaces every superseded row as ``orphaned``.
    """

    @staticmethod
    def _split_with_offset(
        event_service: CalendarEventService,
        social_account: SocialAccount,
        weekly_series: CalendarEvent,
    ) -> CalendarEvent | None:
        """Move the series 30 minutes later, from the second Monday onwards."""
        _grant_event_owner_token(weekly_series, social_account)
        return event_service.create_recurring_event_bulk_modification(
            parent_event=weekly_series,
            modification_start_date=ALL_MONDAYS[1],
            modified_start_time_offset=datetime.timedelta(minutes=30),
        )

    def test_a_fresh_sweep_bills_five_occurrences_for_a_five_occurrence_series(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        social_account: SocialAccount,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """Nothing swept before the edit, so the sweep sees exactly the real series.

        The parent stops at Monday 1 and the continuation picks up from Monday 2 at
        the new time. Five occurrences exist and five are billed.
        """
        self._split_with_offset(event_service, social_account, weekly_series)

        _meter_the_period(metering_service, subscription)

        assert _occurrence_starts(subscription) == [
            ALL_MONDAYS[0],  # 10:00, truncated parent
            ALL_MONDAYS[1] + datetime.timedelta(minutes=30),  # 10:30, continuation
            ALL_MONDAYS[2] + datetime.timedelta(minutes=30),
            ALL_MONDAYS[3] + datetime.timedelta(minutes=30),
            ALL_MONDAYS[4] + datetime.timedelta(minutes=30),
        ]
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5

    def test_a_fresh_sweep_over_a_split_reconciles_clean(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        social_account: SocialAccount,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """Clean here means correct, not blind.

        ``reconcile_period`` compares the ledger against a fresh expansion of the
        same calendar. Both say five — the number of occurrences that actually
        exist — so the period is clean with nothing for finance to act on.
        """
        self._split_with_offset(event_service, social_account, weekly_series)
        _meter_the_period(metering_service, subscription)

        report = metering_service.reconcile_period(subscription, PERIOD_START)

        assert report.expected_count == 5
        assert report.metered_count == 5
        assert report.drift == 0
        assert report.is_clean

    def test_a_split_over_an_already_metered_month_bills_nine(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        social_account: SocialAccount,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """The realistic sweep order, and the identity churn it exposes.

        Metering first, then editing, gives nine rows. The decomposition is now
        entirely identity churn:

        - **5** from the first sweep — Mondays 1-5 at 10:00, correct at the time;
        - **4** added by the second sweep — Mondays 2-5 at 10:30, the moved
          occurrences, which carry a different identity than the 10:00 rows they
          replace.

        The four superseded 10:00 rows are the hazard this design is responsible
        for, and it behaves exactly as documented: bounded by an already-metered
        window, and surfaced as ``orphaned``.
        """
        _meter_the_period(metering_service, subscription)
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5

        self._split_with_offset(event_service, social_account, weekly_series)
        _meter_the_period(metering_service, subscription)

        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 9

    def test_reconciliation_reports_every_superseded_row_as_orphaned(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        social_account: SocialAccount,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """Reconciliation now accounts for the whole over-count, not a fraction of it.

        Nine rows are recorded where five occurrences happened, and all four extra
        rows are reported as drift. ``expected`` is five — the calendar contains
        exactly the real series — so an operator reading ``drift == 4`` sees the
        full size of the discrepancy.
        """
        _meter_the_period(metering_service, subscription)
        self._split_with_offset(event_service, social_account, weekly_series)
        _meter_the_period(metering_service, subscription)

        report = metering_service.reconcile_period(subscription, PERIOD_START)

        assert report.metered_count == 9
        assert report.expected_count == 5
        assert report.drift == 4
        assert [identity.occurrence_start for identity in report.orphaned] == list(ALL_MONDAYS[1:])
        assert report.unmetered == ()

    def test_the_whole_series_stays_attributed_to_the_series_root(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        social_account: SocialAccount,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """The half of identity that *does* hold, isolated from the half that does not.

        Every one of the nine rows — including the spurious ones — is recorded
        under the original master's pk, never the continuation's. The series-root
        walk in ``_resolve_series_root_ids`` works; the over-count is entirely in
        the ``occurrence_start`` component. Worth asserting separately so a future
        reader does not conclude from the wrong totals that root resolution is
        broken and "fix" the wrong thing.
        """
        _meter_the_period(metering_service, subscription)
        continuation = self._split_with_offset(event_service, social_account, weekly_series)
        _meter_the_period(metering_service, subscription)

        assert continuation is not None
        assert continuation.pk != weekly_series.pk
        assert set(
            MeteredOccurrence.objects.filter(subscription=subscription).values_list(
                "event_id", flat=True
            )
        ) == {weekly_series.pk}

    def test_the_splitter_itself_computes_the_split_correctly(
        self,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """Both the arithmetic and the row identity, asserted at the source.

        Split a ``COUNT=5`` series at its second occurrence and the returned pair is
        exactly right: the parent is bounded by ``UNTIL=<Monday #1>`` with no count,
        the continuation carries the remaining four. One plus four is five.

        The last assertion is the one that used to fail. Both halves must be
        *detached* from the original row, or saving either writes over the rule the
        parent is still using.
        """
        from calendar_integration.recurrence_utils import RecurrenceRuleSplitter

        original = weekly_series.recurrence_rule
        assert original is not None, "the fixture series is recurring"
        truncated, continuation = RecurrenceRuleSplitter.split_at_date(
            original, ALL_MONDAYS[1], weekly_series.start_time
        )
        # `split_at_date` returns `None` for either half when that half would
        # generate nothing; a mid-series split produces both.
        assert truncated is not None
        assert continuation is not None

        assert (truncated.count, truncated.until) == (None, ALL_MONDAYS[0])
        assert (continuation.count, continuation.until) == (4, None)
        assert truncated.pk is None
        assert continuation.pk is None

    def test_the_parents_rule_row_holds_the_truncation(
        self,
        event_service: CalendarEventService,
        social_account: SocialAccount,
        subscription: Subscription,
        weekly_series: CalendarEvent,
    ):
        """The persisted evidence for the split, read back from the database.

        After the split the parent still points at its original rule row, and that
        row now holds the truncation — ``UNTIL=<Monday #1>`` with no count. The
        continuation has a rule row of its own, built from an rrule string, so
        neither side can write over the other.
        """
        from calendar_integration.models import RecurrenceRule

        original_rule_id = weekly_series.recurrence_rule_fk_id
        continuation = self._split_with_offset(event_service, social_account, weekly_series)
        assert continuation is not None

        parent = CalendarEvent.objects.filter(organization=subscription.organization).get(
            pk=weekly_series.pk
        )

        assert parent.recurrence_rule_fk_id == original_rule_id, (
            "the parent keeps its original rule row"
        )
        assert continuation.recurrence_rule_fk_id != original_rule_id, (
            "the continuation gets a fresh rule row, built from an rrule string"
        )
        parent_rule = RecurrenceRule.objects.filter(organization=subscription.organization).get(
            pk=original_rule_id
        )
        assert (parent_rule.count, parent_rule.until) == (None, ALL_MONDAYS[0]), (
            "the parent's rule row holds the truncation, bounded at the last "
            "occurrence before the split"
        )

    def test_an_open_ended_series_is_bounded_by_the_split(
        self,
        metering_service: MeteringService,
        event_service: CalendarEventService,
        social_account: SocialAccount,
        subscription: Subscription,
        calendar: Calendar,
    ):
        """The shape that used to be unbounded, and the reason this defect mattered.

        An open-ended series has ``count=None`` and ``until=None``, so the
        continuation the splitter derives is byte-for-byte the *original* unbounded
        rule. When the two shared a row, saving the continuation did not merely move
        the boundary — it erased the truncation, the parent never stopped, and the
        series duplicated **forever**, every future month billed twice.

        It is the shape most real standing meetings have, since an open-ended weekly
        series is the default way to express "every Monday until further notice",
        which is what made this the severe case. The parent must now carry an
        ``UNTIL`` even though the rule it was cloned from had none.
        """
        series = CalendarEventFactory.create_recurring_event(
            calendar=calendar,
            title="Open ended standup",
            description="",
            start_time=FIRST_MONDAY,
            end_time=FIRST_MONDAY + datetime.timedelta(hours=1),
            frequency=RecurrenceFrequency.WEEKLY,
            by_weekday="MO",
            external_id="open_ended_bulk_mod",
        )
        _grant_event_owner_token(series, social_account)
        event_service.create_recurring_event_bulk_modification(
            parent_event=series,
            modification_start_date=ALL_MONDAYS[1],
            modified_start_time_offset=datetime.timedelta(minutes=30),
        )

        parent = CalendarEvent.objects.filter(organization=subscription.organization).get(
            pk=series.pk
        )
        assert (parent.recurrence_rule.count, parent.recurrence_rule.until) == (
            None,
            ALL_MONDAYS[0],
        ), "the once-unbounded parent now stops at the last occurrence before the split"

        _meter_the_period(metering_service, subscription)

        # Monday 1 at 10:00 from the parent, then 10:30 from the continuation.
        assert _occurrence_starts(subscription) == [
            ALL_MONDAYS[0],
            *[monday + datetime.timedelta(minutes=30) for monday in ALL_MONDAYS[1:]],
        ]
        assert MeteredOccurrence.objects.filter(subscription=subscription).count() == 5
