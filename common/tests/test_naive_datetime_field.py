"""Unit tests for ``common.fields.NaiveDateTimeField``.

The field exists so the six deliberately-naive ``*_tz_unaware`` columns on
``RecurringMixin`` stop emitting Django's "received a naive datetime" warning on
every write. Two things have to hold for that to be a safe swap, and both are
asserted here:

1. The warning is gone for a naive value, and still fires for a plain
   ``DateTimeField`` next to it -- so the change is scoped to this field and does
   not blunt the warning everywhere else.
2. The value written is byte-for-byte the one a plain ``DateTimeField`` would have
   written. Django's warning path ends in ``make_aware(value, get_default_timezone())``;
   this field ends in ``replace(tzinfo=UTC)``. With ``TIME_ZONE = "UTC"`` those are
   the same instant, which is what makes the accompanying migration a no-op.
"""

from __future__ import annotations

import datetime
import warnings

from django.db import models

import pytest

from calendar_integration.models import CalendarEvent
from common.fields import NaiveDateTimeField


NAIVE = datetime.datetime(2025, 6, 21, 9, 0)
AWARE = datetime.datetime(2025, 6, 21, 9, 0, tzinfo=datetime.UTC)


def _prep(field: models.DateTimeField, value):
    """``get_prep_value`` on a field bound to a model, capturing any warning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        prepped = field.get_prep_value(value)
    return prepped, [str(w.message) for w in caught]


@pytest.fixture()
def naive_field() -> NaiveDateTimeField:
    field = NaiveDateTimeField()
    field.set_attributes_from_name("probe")
    return field


@pytest.fixture()
def stock_field() -> models.DateTimeField:
    field = models.DateTimeField()
    field.set_attributes_from_name("probe")
    return field


def test_naive_value_is_prepared_as_utc_without_warning(naive_field):
    prepped, caught = _prep(naive_field, NAIVE)

    assert prepped == AWARE
    assert prepped.utcoffset() == datetime.timedelta(0)
    assert caught == []


def test_stock_datetimefield_still_warns_on_the_same_value(stock_field):
    """The control: the warning is only silenced where the field opts in."""
    prepped, caught = _prep(stock_field, NAIVE)

    assert prepped == AWARE
    assert len(caught) == 1
    assert "received a naive datetime" in caught[0]


def test_prepared_value_matches_stock_datetimefield(naive_field, stock_field):
    """Same instant as the behaviour being replaced -- hence the no-op migration."""
    naive_prepped, _ = _prep(naive_field, NAIVE)
    stock_prepped, _ = _prep(stock_field, NAIVE)

    assert naive_prepped == stock_prepped


def test_aware_value_is_left_alone(naive_field):
    prepped, caught = _prep(naive_field, datetime.datetime(2025, 6, 21, 6, 0, tzinfo=datetime.UTC))

    assert prepped == datetime.datetime(2025, 6, 21, 6, 0, tzinfo=datetime.UTC)
    assert caught == []


def test_none_is_left_alone(naive_field):
    prepped, caught = _prep(naive_field, None)

    assert prepped is None
    assert caught == []


def test_naive_iso_string_is_prepared_as_utc(naive_field):
    """Strings reach ``get_prep_value`` from ``.filter(field="...")`` lookups."""
    prepped, caught = _prep(naive_field, "2025-06-21T09:00:00")

    assert prepped == AWARE
    assert caught == []


def test_db_type_is_unchanged_from_datetimefield():
    """What makes migration 0049 emit no SQL."""
    from django.db import connection

    assert NaiveDateTimeField().db_type(connection) == models.DateTimeField().db_type(connection)
    assert NaiveDateTimeField().get_internal_type() == models.DateTimeField().get_internal_type()


def test_recurring_mixin_columns_use_the_field():
    """The regression guard: reverting a column to ``DateTimeField`` fails here."""
    for name in ("start_time_tz_unaware", "end_time_tz_unaware"):
        assert isinstance(CalendarEvent._meta.get_field(name), NaiveDateTimeField)


@pytest.mark.django_db
def test_saving_a_naive_value_emits_no_warning_and_round_trips():
    """End to end: the write that used to warn, through a real model save."""
    from model_bakery import baker

    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        event = CalendarEvent.objects.create(
            calendar_fk=calendar,
            organization=org,
            title="Naive write",
            description="",
            start_time_tz_unaware=NAIVE,
            end_time_tz_unaware=datetime.datetime(2025, 6, 21, 10, 0),
            timezone="UTC",
        )

    assert [str(w.message) for w in caught if "naive datetime" in str(w.message)] == []

    stored = CalendarEvent.objects.filter_by_organization(org.id).get(id=event.id)
    # The column is `timestamptz`, so a read comes back aware-in-UTC -- unchanged by
    # this field, and the point of the `start_time` generated column below.
    assert stored.start_time_tz_unaware == AWARE
    assert stored.start_time == AWARE


@pytest.mark.django_db
def test_model_bakery_can_build_a_model_carrying_the_field():
    """model-bakery matches a generator by the field's exact class, never by its base.

    So subclassing ``DateTimeField`` broke every ``baker.make()`` of a model with one
    of these columns (65 failures and 18 errors across the suite) until
    ``BAKER_CUSTOM_FIELDS_GEN`` in the base settings registered a generator. This is
    the guard for that registration.
    """
    from django.utils import timezone as django_timezone

    from model_bakery import baker

    org = baker.make("organizations.Organization")
    # `timezone` has to be a real IANA name: `start_time` is a generated column and
    # the `convert_naive_utc_to_timezone` function behind it rejects baker's random
    # string. That is pre-existing and unrelated to the field under test.
    event = baker.make("calendar_integration.CalendarEvent", organization=org, timezone="UTC")

    assert django_timezone.is_naive(event.start_time_tz_unaware)
    assert django_timezone.is_naive(event.end_time_tz_unaware)
