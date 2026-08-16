"""A cross-organization row does not join through an organization-safe relation.

This is the core isolation guarantee organization-safe relations exist to
provide, so it is tested against a row that actually points across
organizations rather than against the absence of one.

**How the row is made.** No supported write path can produce it -- assigning
``event.calendar = other_orgs_calendar`` copies the target's organization onto
the event, which is the point of the field. So the fixtures write the concrete
column on its own (``update(calendar_fk_id=...)``, which touches the key and
nothing else), reproducing exactly what a bad data migration, a raw ``UPDATE``,
or a row created while a different organization was selected would leave behind.

**"Missing" has two shapes.** Django's forward descriptor assumes the database
enforces the foreign key, so ``get_object`` lets the ``DoesNotExist`` out rather
than returning ``None`` -- a dangling *safe* relation is a state a plain
``ForeignKey`` cannot reach, and Django has no branch for it. So a bare
``event.calendar`` raises ``Calendar.DoesNotExist`` while
``select_related("calendar")`` yields ``None``. Both are pinned below: what
matters is that neither hands back the other organization's row, and a caller
cannot mistake either for one.

**How this test can fail.** Every assertion about the safe relation
(``calendar``) is paired with the same assertion about the concrete foreign key
(``calendar_fk``), which is an ordinary ``ForeignKey`` and joins on the key
alone. The concrete half *does* reach the row; the safe half does not. So the
file cannot pass by accident on an empty database or on a row that was never
really cross-organization -- the control assertions would go red first. (Proven
the other way too, by hand: reverting ``CalendarEvent.calendar`` to a plain
``models.ForeignKey`` turns every ``TestTheSafeRelationRefusesToJoin`` case red
while the controls stay green.)
"""

from __future__ import annotations

import datetime

import pytest

from calendar_integration.models import Calendar, CalendarEvent, EventAttendance
from organizations.models import Organization


pytestmark = pytest.mark.django_db


@pytest.fixture
def organization_a() -> Organization:
    return Organization.objects.create(name="Org A")


@pytest.fixture
def organization_b() -> Organization:
    return Organization.objects.create(name="Org B")


@pytest.fixture
def calendar_a(organization_a: Organization) -> Calendar:
    return Calendar.objects.create(name="A's calendar", organization=organization_a)


@pytest.fixture
def event_of_b_pointing_at_as_calendar(
    organization_b: Organization, calendar_a: Calendar
) -> CalendarEvent:
    """An event owned by organization B whose ``calendar_fk`` names A's calendar."""
    calendar_b = Calendar.objects.create(name="B's calendar", organization=organization_b)
    event = CalendarEvent.objects.create(
        organization=organization_b,
        calendar=calendar_b,
        title="B's event",
        start_time_tz_unaware=datetime.datetime(2025, 6, 22, 10, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 22, 11, 0),
        timezone="UTC",
    )
    # Writes the key column only -- ``organization_id`` stays B's.
    CalendarEvent.original_manager.filter(pk=event.pk).update(calendar_fk_id=calendar_a.pk)
    event.refresh_from_db()
    return event


class TestTheFixtureReallyIsCrossOrganization:
    """Guards against the whole file passing vacuously."""

    def test_the_row_points_across_organizations_at_the_column_level(
        self, event_of_b_pointing_at_as_calendar, calendar_a, organization_b
    ):
        event = event_of_b_pointing_at_as_calendar

        assert event.calendar_fk_id == calendar_a.pk
        assert event.organization_id == organization_b.pk
        assert calendar_a.organization_id != event.organization_id


class TestTheSafeRelationRefusesToJoin:
    def test_traversing_the_relation_reads_as_missing(self, event_of_b_pointing_at_as_calendar):
        event = CalendarEvent.original_manager.get(pk=event_of_b_pointing_at_as_calendar.pk)

        with pytest.raises(Calendar.DoesNotExist):
            event.calendar  # noqa: B018 -- the attribute access is the assertion
        # Control: the concrete foreign key joins on the key alone and does reach it.
        assert event.calendar_fk is not None

    def test_select_related_yields_nothing_for_the_relation(
        self, event_of_b_pointing_at_as_calendar
    ):
        event = CalendarEvent.original_manager.select_related("calendar").get(
            pk=event_of_b_pointing_at_as_calendar.pk
        )

        assert event.calendar is None
        # Control.
        control = CalendarEvent.original_manager.select_related("calendar_fk").get(
            pk=event_of_b_pointing_at_as_calendar.pk
        )
        assert control.calendar_fk is not None

    def test_filtering_by_the_target_instance_does_not_match_the_row(
        self, event_of_b_pointing_at_as_calendar, calendar_a
    ):
        assert not CalendarEvent.original_manager.filter(calendar=calendar_a).exists()
        # Control.
        assert CalendarEvent.original_manager.filter(calendar_fk=calendar_a).exists()

    def test_filtering_across_the_relation_does_not_match_the_row(
        self, event_of_b_pointing_at_as_calendar, calendar_a
    ):
        assert not CalendarEvent.original_manager.filter(calendar__name=calendar_a.name).exists()
        # Control.
        assert CalendarEvent.original_manager.filter(calendar_fk__name=calendar_a.name).exists()

    def test_the_reverse_accessor_does_not_hand_the_row_back(
        self, event_of_b_pointing_at_as_calendar, calendar_a
    ):
        assert list(calendar_a.events.all()) == []
        # Control: the concrete foreign key's own reverse accessor does.
        assert list(calendar_a.events_fk_rel.all()) == [event_of_b_pointing_at_as_calendar]

    def test_it_reads_as_missing_rather_than_as_the_other_tenants_row(
        self, event_of_b_pointing_at_as_calendar, calendar_a
    ):
        """The distinction that matters: never "some other organization's
        calendar". Under ``select_related`` that is ``None`` -- a serializer
        renders an empty field -- and on a bare attribute access it is
        ``DoesNotExist``. The row is right there under the concrete key, and
        neither shape returns it.
        """
        event = CalendarEvent.original_manager.select_related("calendar").get(
            pk=event_of_b_pointing_at_as_calendar.pk
        )

        assert event.calendar is None
        assert event.calendar_fk.name == calendar_a.name


class TestTheSameHoldsForANestedRelation:
    def test_an_attendance_cannot_reach_an_event_in_another_organization(
        self, organization_a, organization_b, calendar_a
    ):
        event_of_a = CalendarEvent.objects.create(
            organization=organization_a,
            calendar=calendar_a,
            title="A's event",
            start_time_tz_unaware=datetime.datetime(2025, 6, 22, 10, 0),
            end_time_tz_unaware=datetime.datetime(2025, 6, 22, 11, 0),
            timezone="UTC",
        )
        attendance_of_b = EventAttendance.objects.create(organization=organization_b)
        EventAttendance.original_manager.filter(pk=attendance_of_b.pk).update(
            event_fk_id=event_of_a.pk
        )
        attendance_of_b.refresh_from_db()

        with pytest.raises(CalendarEvent.DoesNotExist):
            attendance_of_b.event  # noqa: B018 -- the attribute access is the assertion
        assert (
            EventAttendance.original_manager.select_related("event")
            .get(pk=attendance_of_b.pk)
            .event
            is None
        )
        # Control.
        assert attendance_of_b.event_fk_id == event_of_a.pk
        assert not EventAttendance.original_manager.filter(event=event_of_a).exists()
        assert EventAttendance.original_manager.filter(event_fk=event_of_a).exists()


class TestTheManyToManyJoinsOnTheOrganizationToo:
    """``CalendarEvent.external_attendees`` goes through the *scoped* through model.

    It was the last many-to-many on a scoped model with an auto-created through
    table -- two bare key columns, no ``organization`` -- so its join carried no
    organization and the related manager a many-to-many builds does not add one
    (see ``common.managers.OrganizationScopedManager``). It was repointed at
    ``EventExternalAttendance`` with ``through_fields`` naming the safe relations,
    which puts the organization into both hops' ``ON`` clause.

    Both hops get an intruder. One row is stopped at ``event`` and one at
    ``external_attendee``: an assertion that only planted the first would pass
    with ``through_fields`` still naming the concrete ``external_attendee_fk``,
    which is the column that repoint changed.

    Same construction as the rest of this file: the cross-organization rows are
    written at the column level, because no supported write path can produce them.
    """

    def test_a_cross_organization_attendance_is_not_traversed(
        self, organization_a, organization_b, calendar_a
    ):
        from calendar_integration.models import EventExternalAttendance, ExternalAttendee

        event_of_a = CalendarEvent.objects.create(
            organization=organization_a,
            calendar=calendar_a,
            title="A's event",
            start_time_tz_unaware=datetime.datetime(2025, 6, 22, 10, 0),
            end_time_tz_unaware=datetime.datetime(2025, 6, 22, 11, 0),
            timezone="UTC",
        )
        attendee_of_a = ExternalAttendee.objects.create(
            organization=organization_a, email="guest@example.com", name="Guest"
        )
        attendee_of_b = ExternalAttendee.objects.create(
            organization=organization_b, email="intruder@example.com", name="Intruder"
        )

        # The legitimate row: same organization on all three.
        EventExternalAttendance.objects.create(
            organization=organization_a,
            event=event_of_a,
            external_attendee_fk=attendee_of_a,
        )
        # Intruder 1 -- excluded at the *first* hop (``event``): the through row
        # belongs to B while the event it points at belongs to A.
        intruder_link = EventExternalAttendance.objects.create(
            organization=organization_b,
            external_attendee_fk=attendee_of_b,
        )
        EventExternalAttendance.original_manager.filter(pk=intruder_link.pk).update(
            event_fk_id=event_of_a.pk
        )
        # Intruder 2 -- reaches the first hop and must be stopped at the
        # *second* (``external_attendee``). The through row belongs to A and
        # names A's event, so nothing about the ``event`` join excludes it; only
        # ``through_fields=("event", "external_attendee")`` naming the safe
        # relation puts ``organization`` into the attendee side's ``ON`` clause
        # and drops it. Written through ``external_attendee_fk`` -- the concrete
        # column -- because the safe relation would have copied B's organization
        # onto the row and turned this back into intruder 1.
        second_hop_intruder = EventExternalAttendance.objects.create(
            organization=organization_a,
            event=event_of_a,
            external_attendee_fk=attendee_of_b,
        )

        # Control: the concrete keys *do* reach all three rows, so the assertions
        # below cannot pass merely because the intruder rows are absent. Two of
        # the three belong to A, so neither is excluded by the implicit scope
        # either.
        assert (
            EventExternalAttendance.original_manager.filter(event_fk_id=event_of_a.pk).count() == 3  # noqa: PLR2004
        )
        assert (
            EventExternalAttendance.objects.filter_by_organization(organization_a)
            .filter(event_fk_id=event_of_a.pk)
            .count()
            == 2  # noqa: PLR2004
        )
        assert second_hop_intruder.external_attendee_fk_id == attendee_of_b.pk

        assert list(event_of_a.external_attendees.all()) == [attendee_of_a]
        # ...and the second hop is not traversable on its own either: the row is
        # visible (it is A's), but the attendee it names is not reachable through
        # the safe relation.
        with pytest.raises(ExternalAttendee.DoesNotExist):
            second_hop_intruder.external_attendee  # noqa: B018 -- the access is the assertion
        assert not EventExternalAttendance.original_manager.filter(
            pk=second_hop_intruder.pk, external_attendee=attendee_of_b
        ).exists()
        # Control for the line above: the *concrete* column does reach it.
        assert EventExternalAttendance.original_manager.filter(
            pk=second_hop_intruder.pk, external_attendee_fk=attendee_of_b
        ).exists()

        # ``external_attendances`` is the reverse of the first hop, so it lists
        # both of A's rows -- including the one whose attendee is unreachable.
        assert set(event_of_a.external_attendances.all()) == {
            EventExternalAttendance.original_manager.get(
                event_fk_id=event_of_a.pk, external_attendee_fk=attendee_of_a
            ),
            second_hop_intruder,
        }
