"""Integration tests for the codeless branch of
``POST /public/booking/appointment-types/<public_slug>/events/``.

Phase 3 adds a second authorization path to the Phase 2 endpoint: when
``X-Booking-Code`` is absent, the request is authorized entirely by the path
appointment type's own ``accepts_public_scheduling`` flag, mirroring GraphQL's codeless
``createAppointmentTypeEvent`` mutation. See
``test_booking_rest_create_appointment_type_event.py`` for the coded-path coverage this
complements -- the two files together cover the endpoint's full contract.

All requests here are unauthenticated (no session/JWT, no header at all).

Phase 3b: the path segment addresses ``AppointmentType.public_booking_slug`` --
an opaque, unguessable, globally-unique identifier -- rather than the integer
primary key Phase 3 originally used. Phase 3's integer-keyed route was a
cross-tenant enumeration oracle: with no ``organization_id`` anywhere in this
surface's paths, an anonymous caller could walk ``appointment_type_id`` 1..N and learn,
from the 404/403/201 split, which appointment types exist in ANY organization and which
accept public scheduling. ``TestIntegerKeyedRouteNoLongerResolvesAAppointmentType``
below proves that oracle is gone, not merely harder to exploit -- an integer
path segment still matches the route (digits are inside the slug charset
``[-a-zA-Z0-9_]+``) and reaches the view, it just never resolves to an appointment type
anymore.
"""

import datetime

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from calendar_integration.booking_auth import BOOKING_CODE_HEADER
from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    EventManagementPermissions,
)
from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization


BOOKING_START = datetime.datetime(2030, 7, 1, 10, 0, tzinfo=datetime.UTC)
BOOKING_END = datetime.datetime(2030, 7, 1, 11, 0, tzinfo=datetime.UTC)


def _booking_url(public_slug: str) -> str:
    return f"/public/booking/appointment-types/{public_slug}/events/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="REST Codeless AppointmentType-Book Test Org")


@pytest.fixture
def other_organization():
    return baker.make(Organization, name="Other Org")


def _make_calendar(organization: Organization, external_id: str) -> Calendar:
    return baker.make(
        Calendar,
        organization=organization,
        external_id=external_id,
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        accepts_public_scheduling=False,
    )


def _make_appointment_type_with_two_slots(
    organization: Organization,
    *,
    accepts_public_scheduling: bool,
    primary_calendar: Calendar,
    secondary_calendar: Calendar,
    name: str = "Test AppointmentType",
    duration: datetime.timedelta | None = None,
) -> AppointmentType:
    # A publicly schedulable appointment type must carry a duration --
    # ``can_perform_appointment_type_scheduling`` fails closed (403) for a public appointment type
    # with ``duration=None``, treating it as misconfigured rather than
    # unbounded-length. ``BOOKING_START``/``BOOKING_END`` below span exactly
    # one hour, so that is the default here -- tests that need a different
    # pin override ``appointment_type.duration`` explicitly afterwards.
    if duration is None and accepts_public_scheduling:
        duration = datetime.timedelta(hours=1)
    grp = baker.make(
        AppointmentType,
        organization=organization,
        name=name,
        accepts_public_scheduling=accepts_public_scheduling,
        duration=duration,
    )
    slot_a = AppointmentTypeSlot.objects.create(
        organization=organization,
        appointment_type=grp,
        name="Physicians",
        order=0,
        required_count=1,
    )
    slot_b = AppointmentTypeSlot.objects.create(
        organization=organization, appointment_type=grp, name="Rooms", order=1, required_count=1
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=slot_a, calendar=primary_calendar
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=slot_b, calendar=secondary_calendar
    )
    return grp


@pytest.fixture
def primary_calendar(organization):
    return _make_calendar(organization, "rest-codeless-primary-cal")


@pytest.fixture
def secondary_calendar(organization):
    return _make_calendar(organization, "rest-codeless-room-cal")


@pytest.fixture
def public_appointment_type(organization, primary_calendar, secondary_calendar):
    """An AppointmentType that accepts public (codeless) scheduling."""
    return _make_appointment_type_with_two_slots(
        organization,
        accepts_public_scheduling=True,
        primary_calendar=primary_calendar,
        secondary_calendar=secondary_calendar,
        name="Public AppointmentType",
    )


@pytest.fixture
def private_appointment_type(organization, primary_calendar, secondary_calendar):
    """An AppointmentType that does NOT accept public scheduling -- codeless requests
    against it must be denied."""
    return _make_appointment_type_with_two_slots(
        organization,
        accepts_public_scheduling=False,
        primary_calendar=primary_calendar,
        secondary_calendar=secondary_calendar,
        name="Private AppointmentType",
    )


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def public_appointment_type_booking_code(permission_service, organization, public_appointment_type):
    """A valid single-use CREATE code scoped to `public_appointment_type` -- used to prove
    the coded path wins even though the appointment type itself accepts public scheduling."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        appointment_type_id=public_appointment_type.id,
    )
    return token, code


@pytest.fixture
def anon_client():
    return APIClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slot_selections(
    appointment_type: AppointmentType, primary_calendar: Calendar, secondary_calendar: Calendar
):
    slot_a = appointment_type.slots.get(name="Physicians")
    slot_b = appointment_type.slots.get(name="Rooms")
    return [
        {"slot_id": slot_a.id, "calendar_ids": [primary_calendar.id]},
        {"slot_id": slot_b.id, "calendar_ids": [secondary_calendar.id]},
    ]


def _appointment_type_booking_payload(slot_selections: list[dict], **overrides) -> dict:
    base = {
        "title": "Codeless AppointmentType Appointment",
        "description": "A codeless appointment type booking",
        "start_time": BOOKING_START.isoformat(),
        "end_time": BOOKING_END.isoformat(),
        "timezone": "UTC",
        "slot_selections": slot_selections,
        "external_attendee": {
            "email": "patient@example.com",
            "name": "Pat Patient",
        },
    }
    base.update(overrides)
    return base


def _post(client: APIClient, public_slug: str, code: str | None, payload: dict):
    headers = {BOOKING_CODE_HEADER: code} if code is not None else None
    return client.post(_booking_url(public_slug), payload, format="json", headers=headers)


# ---------------------------------------------------------------------------
# Scenario 1: Codeless happy path against a public appointment type
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessAppointmentTypeEventHappyPath:
    def test_public_appointment_type_books_with_no_header(
        self,
        anon_client,
        organization,
        public_appointment_type,
        primary_calendar,
        secondary_calendar,
    ):
        selections = _slot_selections(public_appointment_type, primary_calendar, secondary_calendar)

        response = _post(
            anon_client,
            public_appointment_type.public_booking_slug,
            None,
            _appointment_type_booking_payload(selections),
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()
        assert body["title"] == "Codeless AppointmentType Appointment"

        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=body["id"])
        assert event.calendar_fk_id == primary_calendar.id
        assert event.appointment_type_fk_id == public_appointment_type.id
        assert event.organization_id == organization.id

    def test_no_code_is_consumed(
        self,
        anon_client,
        permission_service,
        organization,
        public_appointment_type,
        primary_calendar,
        secondary_calendar,
    ):
        """No code is presented, so none can be consumed -- asserted explicitly.

        A valid appointment type booking code exists for `public_appointment_type` (it could have been
        used to book this exact request) but is never sent. The codeless request
        must still succeed via the appointment type's own ``accepts_public_scheduling``, and
        that unrelated, unpresented code must remain completely untouched."""
        unused_token, _unused_code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            appointment_type_id=public_appointment_type.id,
        )

        selections = _slot_selections(public_appointment_type, primary_calendar, secondary_calendar)
        response = _post(
            anon_client,
            public_appointment_type.public_booking_slug,
            None,
            _appointment_type_booking_payload(selections),
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content

        unused_token.refresh_from_db()
        assert unused_token.used_at is None
        assert unused_token.consumed_source_ip is None

    def test_existing_tokens_in_the_organization_are_left_untouched(
        self,
        anon_client,
        permission_service,
        organization,
        public_appointment_type,
        primary_calendar,
        secondary_calendar,
    ):
        """A codeless booking must not read, consume, or otherwise mutate any
        PRE-EXISTING CalendarManagementToken row -- there is no code in the
        request to resolve one against. Seed a handful of unrelated live
        booking-code tokens (calendar-scoped and appointment-type-scoped) and prove every
        one of them is byte-identical after the codeless request.

        This does not assert the organization's token count is unchanged:
        ``create_appointment_type_event`` always mints a fresh per-attendee RSVP
        management token for the new event's external attendee, regardless of
        whether the booking was coded or codeless -- that is an unrelated,
        expected side effect of event creation, not a booking code being
        consumed."""
        calendar_token, _ = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=primary_calendar.id,
        )
        appointment_type_token, _ = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            appointment_type_id=public_appointment_type.id,
        )
        pre_existing_ids = {calendar_token.id, appointment_type_token.id}
        before = {
            token.id: (token.used_at, token.consumed_source_ip, token.revoked_at)
            for token in CalendarManagementToken.objects.filter_by_organization(
                organization.id
            ).filter(id__in=pre_existing_ids)
        }
        assert len(before) == 2

        selections = _slot_selections(public_appointment_type, primary_calendar, secondary_calendar)
        response = _post(
            anon_client,
            public_appointment_type.public_booking_slug,
            None,
            _appointment_type_booking_payload(selections),
        )
        assert response.status_code == status.HTTP_201_CREATED, response.content

        after = {
            token.id: (token.used_at, token.consumed_source_ip, token.revoked_at)
            for token in CalendarManagementToken.objects.filter_by_organization(
                organization.id
            ).filter(id__in=pre_existing_ids)
        }
        assert after == before

        calendar_token.refresh_from_db()
        appointment_type_token.refresh_from_db()
        assert calendar_token.used_at is None
        assert appointment_type_token.used_at is None


# ---------------------------------------------------------------------------
# Scenario 2: Codeless denial against a private appointment type
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessAppointmentTypeEventPrivateAppointmentTypeDenied:
    def test_private_appointment_type_returns_not_permitted(
        self,
        anon_client,
        organization,
        private_appointment_type,
        primary_calendar,
        secondary_calendar,
    ):
        selections = _slot_selections(
            private_appointment_type, primary_calendar, secondary_calendar
        )

        response = _post(
            anon_client,
            private_appointment_type.public_booking_slug,
            None,
            _appointment_type_booking_payload(selections),
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        body = response.json()
        assert body["error_code"] == "NOT_PERMITTED"
        assert "does not accept public scheduling" in body["detail"].lower()

    def test_private_appointment_type_books_nothing(
        self,
        anon_client,
        organization,
        private_appointment_type,
        primary_calendar,
        secondary_calendar,
    ):
        selections = _slot_selections(
            private_appointment_type, primary_calendar, secondary_calendar
        )

        response = _post(
            anon_client,
            private_appointment_type.public_booking_slug,
            None,
            _appointment_type_booking_payload(selections),
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 3: Missing appointment type returns 404 (not a secret on this path).
# Phase 3b: the identifier is now an unguessable slug, so a bare 404 for one
# that resolves to no appointment type discloses nothing exploitable -- unlike the old
# integer id, which let a 404/403/201 split enumerate real appointment types.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessAppointmentTypeEventMissingAppointmentType:
    def test_well_formed_but_nonexistent_slug_returns_404(self, anon_client):
        response = _post(
            anon_client,
            "well-formed-but-nonexistent-slug",
            None,
            _appointment_type_booking_payload([]),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_malformed_slug_and_nonexistent_slug_both_404_with_different_bodies(self, anon_client):
        """A path segment outside the slug charset (``[-a-zA-Z0-9_]+`` -- here
        one containing dots) never even reaches the view; the router itself
        has no matching pattern, so Django's own URL resolver produces the
        404 (an HTML body here, since ``DEBUG=True`` in tests). A well-formed
        slug that simply resolves to no appointment type instead reaches
        ``BookingCodeAppointmentTypeEventViewSet``, which raises DRF's ``NotFound`` --
        a JSON body. The two responses are NOT byte-identical, unlike the
        docstring on this test previously claimed -- but the difference is
        harmless: a malformed segment can never be a real
        ``public_booking_slug`` in the first place (the charset excludes it),
        so which of the two 404 shapes comes back leaks nothing about
        whether any *real* slug exists. Both branches simply agree on the
        status code."""
        malformed_response = anon_client.post(
            "/public/booking/appointment-types/not.a.valid.slug/events/",
            _appointment_type_booking_payload([]),
            format="json",
        )
        wellformed_response = _post(
            anon_client,
            "another-well-formed-but-nonexistent-slug",
            None,
            _appointment_type_booking_payload([]),
        )

        assert malformed_response.status_code == status.HTTP_404_NOT_FOUND
        assert wellformed_response.status_code == status.HTTP_404_NOT_FOUND

        # Bodies deliberately differ: Django's un-routed-URL 404 (HTML) vs.
        # DRF's `NotFound` (JSON `{"detail": ...}`) -- see the docstring above
        # for why that difference discloses nothing.
        assert wellformed_response.json() == {"detail": "Appointment type not found."}
        assert malformed_response["Content-Type"] != wellformed_response["Content-Type"]


# ---------------------------------------------------------------------------
# Scenario 4: The coded branch wins when the header is present, even against a
# appointment type that itself accepts public scheduling.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodedBranchWinsOverCodeless:
    def test_valid_code_against_public_appointment_type_still_consumes_the_code(
        self,
        anon_client,
        public_appointment_type_booking_code,
        organization,
        public_appointment_type,
        primary_calendar,
        secondary_calendar,
    ):
        """An appointment type that accepts public scheduling AND is handed a valid appointment type
        code still books through the coded path -- and that code IS consumed.
        The coded branch wins whenever the header is present."""
        token, code = public_appointment_type_booking_code
        selections = _slot_selections(public_appointment_type, primary_calendar, secondary_calendar)

        response = _post(
            anon_client,
            public_appointment_type.public_booking_slug,
            code,
            _appointment_type_booking_payload(selections),
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content
        token.refresh_from_db()
        assert token.used_at is not None
        assert token.consumed_source_ip is not None


# ---------------------------------------------------------------------------
# Scenario 5: Cross-organization isolation on the codeless path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessAppointmentTypeEventCrossOrgIsolation:
    def test_codeless_booking_stays_scoped_to_its_own_organization(
        self,
        anon_client,
        organization,
        other_organization,
        public_appointment_type,
        primary_calendar,
        secondary_calendar,
    ):
        """A second organization existing at all must not let a codeless
        booking against `organization`'s public appointment type leak into, or be
        satisfied by, `other_organization`'s data -- the appointment type id alone
        determines the organization, and nothing else can redirect it."""
        other_primary = _make_calendar(other_organization, "rest-codeless-other-primary")
        other_secondary = _make_calendar(other_organization, "rest-codeless-other-room")
        other_appointment_type = _make_appointment_type_with_two_slots(
            other_organization,
            accepts_public_scheduling=True,
            primary_calendar=other_primary,
            secondary_calendar=other_secondary,
            name="Other Org Public AppointmentType",
        )

        selections = _slot_selections(public_appointment_type, primary_calendar, secondary_calendar)
        response = _post(
            anon_client,
            public_appointment_type.public_booking_slug,
            None,
            _appointment_type_booking_payload(selections),
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()

        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=body["id"])
        assert event.organization_id == organization.id
        assert not CalendarEvent.objects.filter_by_organization(other_organization.id).exists()

        other_appointment_type.refresh_from_db()  # sanity: untouched, no event linked to it
        assert (
            not CalendarEvent.objects.filter_by_organization(other_organization.id)
            .filter(appointment_type_fk_id=other_appointment_type.id)
            .exists()
        )


# ---------------------------------------------------------------------------
# Scenario 6: ambiguous X-Booking-Code header values -- empty string vs.
# whitespace-only. ``booking_code_header`` does ``return value or None``, so
# these two must NOT be treated the same: an empty string is falsy (codeless),
# a whitespace-only string is truthy (coded).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAmbiguousHeaderValues:
    def test_empty_header_value_is_treated_as_codeless(
        self,
        anon_client,
        organization,
        public_appointment_type,
        private_appointment_type,
        primary_calendar,
        secondary_calendar,
    ):
        """``booking_code_header`` normalizes an empty-string header to
        ``None`` (``value or None``), so an empty ``X-Booking-Code`` takes the
        codeless branch exactly like an absent header -- proven here against
        both a PUBLIC appointment type (books) and a PRIVATE appointment type (denied via the same
        403 NOT_PERMITTED the fully-absent-header case gets), so the branch
        choice is unambiguous either way."""
        public_selections = _slot_selections(
            public_appointment_type, primary_calendar, secondary_calendar
        )
        public_response = _post(
            anon_client,
            public_appointment_type.public_booking_slug,
            "",
            _appointment_type_booking_payload(public_selections),
        )
        assert public_response.status_code == status.HTTP_201_CREATED, public_response.content

        private_selections = _slot_selections(
            private_appointment_type, primary_calendar, secondary_calendar
        )
        private_response = _post(
            anon_client,
            private_appointment_type.public_booking_slug,
            "",
            _appointment_type_booking_payload(private_selections),
        )
        assert private_response.status_code == status.HTTP_403_FORBIDDEN
        assert private_response.json()["error_code"] == "NOT_PERMITTED"

    def test_whitespace_header_is_treated_as_a_code_not_codeless(
        self,
        anon_client,
        organization,
        public_appointment_type,
        primary_calendar,
        secondary_calendar,
    ):
        """A whitespace-only ``X-Booking-Code`` (``" "``) is truthy, so
        ``booking_code_header`` returns it unchanged and the request takes the
        CODED branch -- never the codeless one, even against an appointment type that
        itself accepts public scheduling. This matters: if a whitespace
        header fell through to codeless, a caller could bypass every one of
        the coded path's checks (resolve/authorize/scope/pin) just by sending
        a blank-looking header instead of omitting it -- that would be a
        bypass of the coded path's guarantees, not a convenience. Instead,
        the coded branch tries to resolve `" "` as a code and fails.

        Observed (not assumed): ``resolve_code`` cannot decode a whitespace
        string into a valid ``token_id:token_str`` pair, so it raises
        ``InvalidTokenError`` -> ``InvalidCodeAPIException`` -> ``404
        INVALID_CODE``.
        """
        selections = _slot_selections(public_appointment_type, primary_calendar, secondary_calendar)

        response = _post(
            anon_client,
            public_appointment_type.public_booking_slug,
            " ",
            _appointment_type_booking_payload(selections),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND, response.content
        assert response.json()["error_code"] == "INVALID_CODE"
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 7: pinned duration applies to the codeless branch too -- the pin
# lives on the AppointmentType, not on a code, so a codeless booking (no
# credential at all) is constrained by it exactly like a coded one.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessAppointmentTypeEventPinnedDuration:
    def test_pinned_duration_books_at_exact_span_with_no_credential(
        self,
        anon_client,
        public_appointment_type,
        primary_calendar,
        secondary_calendar,
    ):
        public_appointment_type.duration = datetime.timedelta(minutes=30)
        public_appointment_type.save()
        selections = _slot_selections(public_appointment_type, primary_calendar, secondary_calendar)
        payload = _appointment_type_booking_payload(
            selections, end_time=(BOOKING_START + datetime.timedelta(minutes=30)).isoformat()
        )

        response = _post(anon_client, public_appointment_type.public_booking_slug, None, payload)

        assert response.status_code == status.HTTP_201_CREATED, response.content

    def test_pinned_duration_refuses_a_different_span_with_no_credential(
        self,
        anon_client,
        organization,
        public_appointment_type,
        primary_calendar,
        secondary_calendar,
    ):
        """The pin lives on the ``AppointmentType``, not on a code -- a codeless
        booking (no ``X-Booking-Code`` header at all) is constrained by it
        exactly like a coded one. This is the entire point of the Phase 0
        rewrite: a codeless booking presents no credential to carry a
        per-code pin, so leaving the constraint on the code would have made
        the one path reachable with no credential the one path with no
        length constraint."""
        public_appointment_type.duration = datetime.timedelta(minutes=30)
        public_appointment_type.save()
        selections = _slot_selections(public_appointment_type, primary_calendar, secondary_calendar)
        # 45-minute span -- does not match the 30-minute pin.
        payload = _appointment_type_booking_payload(
            selections, end_time=(BOOKING_START + datetime.timedelta(minutes=45)).isoformat()
        )

        response = _post(anon_client, public_appointment_type.public_booking_slug, None, payload)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        body = response.json()
        assert body["error_code"] == "NOT_PERMITTED"
        assert "30 minute" in body["detail"]

        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 8: an integer path segment still MATCHES the route -- digits are
# inside the slug charset `[-a-zA-Z0-9_]+` -- but it never again RESOLVES to
# an appointment type. The cross-tenant enumeration oracle Phase 3b exists to close is
# GONE, not merely harder to exploit: probing by a real, existing appointment type's
# OWN integer primary key -- the exact identifier Phase 3 used to expose --
# must never again identify that appointment type, on either branch, and must be
# indistinguishable whether the appointment type is public, private, or the id doesn't
# exist at all.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIntegerKeyedRouteNoLongerResolvesAAppointmentType:
    def test_probing_a_real_public_appointment_types_own_id_codeless_returns_404(
        self, anon_client, public_appointment_type
    ):
        """The exact integer id that used to book `public_appointment_type` codelessly
        pre-Phase-3b now resolves nothing: `public_booking_slug` is looked up,
        not `id`, and no appointment type's slug is ever a bare decimal integer."""
        response = _post(
            anon_client,
            str(public_appointment_type.id),
            None,
            _appointment_type_booking_payload([]),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_probing_a_real_private_appointment_types_own_id_codeless_returns_404(
        self, anon_client, private_appointment_type
    ):
        """Same proof against a PRIVATE appointment type's own id -- previously this
        would have differed from the public case (403 NOT_PERMITTED, since
        the appointment type exists but ``accepts_public_scheduling`` is False). Now
        both are the identical 404: the id no longer identifies any appointment type at
        all, public or private, so there is nothing left for the 404/403
        split to distinguish."""
        response = _post(
            anon_client,
            str(private_appointment_type.id),
            None,
            _appointment_type_booking_payload([]),
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_probing_a_real_appointment_types_own_id_coded_returns_403_not_404(
        self, anon_client, public_appointment_type_booking_code, public_appointment_type
    ):
        """A code minted for `public_appointment_type`, presented against that SAME
        appointment type's own integer id in the path, is still just a mismatch: digits
        are valid slug characters, so the route matches syntactically, but no
        appointment type's slug equals its own numeric id, so the token's own resolved
        slug never matches the path -- 403 NOT_PERMITTED, never 404, exactly
        like any other wrong slug on the coded branch."""
        _token, code = public_appointment_type_booking_code

        response = _post(
            anon_client,
            str(public_appointment_type.id),
            code,
            _appointment_type_booking_payload([]),
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"
