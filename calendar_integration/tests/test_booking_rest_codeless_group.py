"""Integration tests for the codeless branch of
``POST /public/booking/calendar-groups/<public_slug>/events/``.

Phase 3 adds a second authorization path to the Phase 2 endpoint: when
``X-Booking-Code`` is absent, the request is authorized entirely by the path
group's own ``accepts_public_scheduling`` flag, mirroring GraphQL's codeless
``createCalendarGroupEvent`` mutation. See
``test_booking_rest_create_group_event.py`` for the coded-path coverage this
complements -- the two files together cover the endpoint's full contract.

All requests here are unauthenticated (no session/JWT, no header at all).

Phase 3b: the path segment addresses ``CalendarGroup.public_booking_slug`` --
an opaque, unguessable, globally-unique identifier -- rather than the integer
primary key Phase 3 originally used. Phase 3's integer-keyed route was a
cross-tenant enumeration oracle: with no ``organization_id`` anywhere in this
surface's paths, an anonymous caller could walk ``group_id`` 1..N and learn,
from the 404/403/201 split, which groups exist in ANY organization and which
accept public scheduling. ``TestIntegerKeyedRouteNoLongerResolves`` below
proves that oracle is gone, not merely harder to exploit.
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
    Calendar,
    CalendarEvent,
    CalendarGroup,
    CalendarGroupSlot,
    CalendarGroupSlotMembership,
    CalendarManagementToken,
)
from calendar_integration.services.calendar_permission_service import CalendarPermissionService
from organizations.models import Organization


BOOKING_START = datetime.datetime(2030, 7, 1, 10, 0, tzinfo=datetime.UTC)
BOOKING_END = datetime.datetime(2030, 7, 1, 11, 0, tzinfo=datetime.UTC)


def _booking_url(public_slug: str) -> str:
    return f"/public/booking/calendar-groups/{public_slug}/events/"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    return baker.make(Organization, name="REST Codeless Group-Book Test Org")


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


def _make_group_with_two_slots(
    organization: Organization,
    *,
    accepts_public_scheduling: bool,
    primary_calendar: Calendar,
    secondary_calendar: Calendar,
    name: str = "Test Group",
    duration: datetime.timedelta | None = None,
) -> CalendarGroup:
    # A publicly schedulable group must carry a duration --
    # ``can_perform_group_scheduling`` fails closed (403) for a public group
    # with ``duration=None``, treating it as misconfigured rather than
    # unbounded-length. ``BOOKING_START``/``BOOKING_END`` below span exactly
    # one hour, so that is the default here -- tests that need a different
    # pin override ``group.duration`` explicitly afterwards.
    if duration is None and accepts_public_scheduling:
        duration = datetime.timedelta(hours=1)
    grp = baker.make(
        CalendarGroup,
        organization=organization,
        name=name,
        accepts_public_scheduling=accepts_public_scheduling,
        duration=duration,
    )
    slot_a = CalendarGroupSlot.objects.create(
        organization=organization, group=grp, name="Physicians", order=0, required_count=1
    )
    slot_b = CalendarGroupSlot.objects.create(
        organization=organization, group=grp, name="Rooms", order=1, required_count=1
    )
    CalendarGroupSlotMembership.objects.create(
        organization=organization, slot=slot_a, calendar=primary_calendar
    )
    CalendarGroupSlotMembership.objects.create(
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
def public_group(organization, primary_calendar, secondary_calendar):
    """A CalendarGroup that accepts public (codeless) scheduling."""
    return _make_group_with_two_slots(
        organization,
        accepts_public_scheduling=True,
        primary_calendar=primary_calendar,
        secondary_calendar=secondary_calendar,
        name="Public Group",
    )


@pytest.fixture
def private_group(organization, primary_calendar, secondary_calendar):
    """A CalendarGroup that does NOT accept public scheduling -- codeless requests
    against it must be denied."""
    return _make_group_with_two_slots(
        organization,
        accepts_public_scheduling=False,
        primary_calendar=primary_calendar,
        secondary_calendar=secondary_calendar,
        name="Private Group",
    )


@pytest.fixture
def permission_service():
    return CalendarPermissionService()


@pytest.fixture
def public_group_booking_code(permission_service, organization, public_group):
    """A valid single-use CREATE code scoped to `public_group` -- used to prove
    the coded path wins even though the group itself accepts public scheduling."""
    token, code = permission_service.create_booking_token(
        organization_id=organization.id,
        permissions=[EventManagementPermissions.CREATE],
        calendar_group_id=public_group.id,
    )
    return token, code


@pytest.fixture
def anon_client():
    return APIClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slot_selections(
    group: CalendarGroup, primary_calendar: Calendar, secondary_calendar: Calendar
):
    slot_a = group.slots.get(name="Physicians")
    slot_b = group.slots.get(name="Rooms")
    return [
        {"slot_id": slot_a.id, "calendar_ids": [primary_calendar.id]},
        {"slot_id": slot_b.id, "calendar_ids": [secondary_calendar.id]},
    ]


def _group_booking_payload(slot_selections: list[dict], **overrides) -> dict:
    base = {
        "title": "Codeless Group Appointment",
        "description": "A codeless group booking",
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
# Scenario 1: Codeless happy path against a public group
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessGroupEventHappyPath:
    def test_public_group_books_with_no_header(
        self,
        anon_client,
        organization,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)

        response = _post(
            anon_client, public_group.public_booking_slug, None, _group_booking_payload(selections)
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()
        assert body["title"] == "Codeless Group Appointment"

        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=body["id"])
        assert event.calendar_fk_id == primary_calendar.id
        assert event.calendar_group_fk_id == public_group.id
        assert event.organization_id == organization.id

    def test_no_code_is_consumed(
        self,
        anon_client,
        permission_service,
        organization,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        """No code is presented, so none can be consumed -- asserted explicitly.

        A valid group booking code exists for `public_group` (it could have been
        used to book this exact request) but is never sent. The codeless request
        must still succeed via the group's own ``accepts_public_scheduling``, and
        that unrelated, unpresented code must remain completely untouched."""
        unused_token, _unused_code = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=public_group.id,
        )

        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)
        response = _post(
            anon_client, public_group.public_booking_slug, None, _group_booking_payload(selections)
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
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        """A codeless booking must not read, consume, or otherwise mutate any
        PRE-EXISTING CalendarManagementToken row -- there is no code in the
        request to resolve one against. Seed a handful of unrelated live
        booking-code tokens (calendar-scoped and group-scoped) and prove every
        one of them is byte-identical after the codeless request.

        This does not assert the organization's token count is unchanged:
        ``create_grouped_event`` always mints a fresh per-attendee RSVP
        management token for the new event's external attendee, regardless of
        whether the booking was coded or codeless -- that is an unrelated,
        expected side effect of event creation, not a booking code being
        consumed."""
        calendar_token, _ = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_id=primary_calendar.id,
        )
        group_token, _ = permission_service.create_booking_token(
            organization_id=organization.id,
            permissions=[EventManagementPermissions.CREATE],
            calendar_group_id=public_group.id,
        )
        pre_existing_ids = {calendar_token.id, group_token.id}
        before = {
            token.id: (token.used_at, token.consumed_source_ip, token.revoked_at)
            for token in CalendarManagementToken.objects.filter_by_organization(
                organization.id
            ).filter(id__in=pre_existing_ids)
        }
        assert len(before) == 2

        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)
        response = _post(
            anon_client, public_group.public_booking_slug, None, _group_booking_payload(selections)
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
        group_token.refresh_from_db()
        assert calendar_token.used_at is None
        assert group_token.used_at is None


# ---------------------------------------------------------------------------
# Scenario 2: Codeless denial against a private group
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessGroupEventPrivateGroupDenied:
    def test_private_group_returns_not_permitted(
        self,
        anon_client,
        organization,
        private_group,
        primary_calendar,
        secondary_calendar,
    ):
        selections = _slot_selections(private_group, primary_calendar, secondary_calendar)

        response = _post(
            anon_client, private_group.public_booking_slug, None, _group_booking_payload(selections)
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        body = response.json()
        assert body["error_code"] == "NOT_PERMITTED"
        assert "does not accept public scheduling" in body["detail"].lower()

    def test_private_group_books_nothing(
        self,
        anon_client,
        organization,
        private_group,
        primary_calendar,
        secondary_calendar,
    ):
        selections = _slot_selections(private_group, primary_calendar, secondary_calendar)

        response = _post(
            anon_client, private_group.public_booking_slug, None, _group_booking_payload(selections)
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 3: Missing group returns 404 (not a secret on this path).
# Phase 3b: the identifier is now an unguessable slug, so a bare 404 for one
# that resolves to no group discloses nothing exploitable -- unlike the old
# integer id, which let a 404/403/201 split enumerate real groups.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessGroupEventMissingGroup:
    def test_well_formed_but_nonexistent_slug_returns_404(self, anon_client):
        response = _post(
            anon_client, "well-formed-but-nonexistent-slug", None, _group_booking_payload([])
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_malformed_slug_is_indistinguishable_from_a_nonexistent_one(self, anon_client):
        """A path segment outside the slug charset (``[-a-zA-Z0-9_]+`` -- here
        one containing dots) never even reaches the view; the router itself
        has no matching pattern. The response is still a plain 404, exactly
        like a well-formed slug that simply resolves to no group -- neither
        case discloses anything about whether any group, anywhere, uses a
        similar identifier."""
        malformed_response = anon_client.post(
            "/public/booking/calendar-groups/not.a.valid.slug/events/",
            _group_booking_payload([]),
            format="json",
        )
        wellformed_response = _post(
            anon_client,
            "another-well-formed-but-nonexistent-slug",
            None,
            _group_booking_payload([]),
        )

        assert malformed_response.status_code == status.HTTP_404_NOT_FOUND
        assert wellformed_response.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# Scenario 4: The coded branch wins when the header is present, even against a
# group that itself accepts public scheduling.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodedBranchWinsOverCodeless:
    def test_valid_code_against_public_group_still_consumes_the_code(
        self,
        anon_client,
        public_group_booking_code,
        organization,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        """A group that accepts public scheduling AND is handed a valid group
        code still books through the coded path -- and that code IS consumed.
        The coded branch wins whenever the header is present."""
        token, code = public_group_booking_code
        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)

        response = _post(
            anon_client, public_group.public_booking_slug, code, _group_booking_payload(selections)
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content
        token.refresh_from_db()
        assert token.used_at is not None
        assert token.consumed_source_ip is not None


# ---------------------------------------------------------------------------
# Scenario 5: Cross-organization isolation on the codeless path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessGroupEventCrossOrgIsolation:
    def test_codeless_booking_stays_scoped_to_its_own_organization(
        self,
        anon_client,
        organization,
        other_organization,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        """A second organization existing at all must not let a codeless
        booking against `organization`'s public group leak into, or be
        satisfied by, `other_organization`'s data -- the group id alone
        determines the organization, and nothing else can redirect it."""
        other_primary = _make_calendar(other_organization, "rest-codeless-other-primary")
        other_secondary = _make_calendar(other_organization, "rest-codeless-other-room")
        other_group = _make_group_with_two_slots(
            other_organization,
            accepts_public_scheduling=True,
            primary_calendar=other_primary,
            secondary_calendar=other_secondary,
            name="Other Org Public Group",
        )

        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)
        response = _post(
            anon_client, public_group.public_booking_slug, None, _group_booking_payload(selections)
        )

        assert response.status_code == status.HTTP_201_CREATED, response.content
        body = response.json()

        event = CalendarEvent.objects.filter_by_organization(organization.id).get(id=body["id"])
        assert event.organization_id == organization.id
        assert not CalendarEvent.objects.filter_by_organization(other_organization.id).exists()

        other_group.refresh_from_db()  # sanity: untouched, no event linked to it
        assert (
            not CalendarEvent.objects.filter_by_organization(other_organization.id)
            .filter(calendar_group_fk_id=other_group.id)
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
        public_group,
        private_group,
        primary_calendar,
        secondary_calendar,
    ):
        """``booking_code_header`` normalizes an empty-string header to
        ``None`` (``value or None``), so an empty ``X-Booking-Code`` takes the
        codeless branch exactly like an absent header -- proven here against
        both a PUBLIC group (books) and a PRIVATE group (denied via the same
        403 NOT_PERMITTED the fully-absent-header case gets), so the branch
        choice is unambiguous either way."""
        public_selections = _slot_selections(public_group, primary_calendar, secondary_calendar)
        public_response = _post(
            anon_client,
            public_group.public_booking_slug,
            "",
            _group_booking_payload(public_selections),
        )
        assert public_response.status_code == status.HTTP_201_CREATED, public_response.content

        private_selections = _slot_selections(private_group, primary_calendar, secondary_calendar)
        private_response = _post(
            anon_client,
            private_group.public_booking_slug,
            "",
            _group_booking_payload(private_selections),
        )
        assert private_response.status_code == status.HTTP_403_FORBIDDEN
        assert private_response.json()["error_code"] == "NOT_PERMITTED"

    def test_whitespace_header_is_treated_as_a_code_not_codeless(
        self,
        anon_client,
        organization,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        """A whitespace-only ``X-Booking-Code`` (``" "``) is truthy, so
        ``booking_code_header`` returns it unchanged and the request takes the
        CODED branch -- never the codeless one, even against a group that
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
        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)

        response = _post(
            anon_client, public_group.public_booking_slug, " ", _group_booking_payload(selections)
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND, response.content
        assert response.json()["error_code"] == "INVALID_CODE"
        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 7: pinned duration applies to the codeless branch too -- the pin
# lives on the CalendarGroup, not on a code, so a codeless booking (no
# credential at all) is constrained by it exactly like a coded one.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCodelessGroupEventPinnedDuration:
    def test_pinned_duration_books_at_exact_span_with_no_credential(
        self,
        anon_client,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        public_group.duration = datetime.timedelta(minutes=30)
        public_group.save()
        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)
        payload = _group_booking_payload(
            selections, end_time=(BOOKING_START + datetime.timedelta(minutes=30)).isoformat()
        )

        response = _post(anon_client, public_group.public_booking_slug, None, payload)

        assert response.status_code == status.HTTP_201_CREATED, response.content

    def test_pinned_duration_refuses_a_different_span_with_no_credential(
        self,
        anon_client,
        organization,
        public_group,
        primary_calendar,
        secondary_calendar,
    ):
        """The pin lives on the ``CalendarGroup``, not on a code -- a codeless
        booking (no ``X-Booking-Code`` header at all) is constrained by it
        exactly like a coded one. This is the entire point of the Phase 0
        rewrite: a codeless booking presents no credential to carry a
        per-code pin, so leaving the constraint on the code would have made
        the one path reachable with no credential the one path with no
        length constraint."""
        public_group.duration = datetime.timedelta(minutes=30)
        public_group.save()
        selections = _slot_selections(public_group, primary_calendar, secondary_calendar)
        # 45-minute span -- does not match the 30-minute pin.
        payload = _group_booking_payload(
            selections, end_time=(BOOKING_START + datetime.timedelta(minutes=45)).isoformat()
        )

        response = _post(anon_client, public_group.public_booking_slug, None, payload)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        body = response.json()
        assert body["error_code"] == "NOT_PERMITTED"
        assert "30 minute" in body["detail"]

        assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


# ---------------------------------------------------------------------------
# Scenario 8: the integer-keyed path no longer routes at all -- the
# cross-tenant enumeration oracle Phase 3b exists to close is GONE, not
# merely harder to exploit. Probing by a real, existing group's OWN integer
# primary key -- the exact identifier Phase 3 used to expose -- must never
# again resolve that group, on either branch, and must be indistinguishable
# whether the group is public, private, or the id doesn't exist at all.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIntegerKeyedRouteNoLongerResolves:
    def test_probing_a_real_public_groups_own_id_codeless_returns_404(
        self, anon_client, public_group
    ):
        """The exact integer id that used to book `public_group` codelessly
        pre-Phase-3b now resolves nothing: `public_booking_slug` is looked up,
        not `id`, and no group's slug is ever a bare decimal integer."""
        response = _post(anon_client, str(public_group.id), None, _group_booking_payload([]))

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_probing_a_real_private_groups_own_id_codeless_returns_404(
        self, anon_client, private_group
    ):
        """Same proof against a PRIVATE group's own id -- previously this
        would have differed from the public case (403 NOT_PERMITTED, since
        the group exists but ``accepts_public_scheduling`` is False). Now
        both are the identical 404: the id no longer identifies any group at
        all, public or private, so there is nothing left for the 404/403
        split to distinguish."""
        response = _post(anon_client, str(private_group.id), None, _group_booking_payload([]))

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_probing_a_real_groups_own_id_coded_returns_403_not_404(
        self, anon_client, public_group_booking_code, public_group
    ):
        """A code minted for `public_group`, presented against that SAME
        group's own integer id in the path, is still just a mismatch: digits
        are valid slug characters, so the route matches syntactically, but no
        group's slug equals its own numeric id, so the token's own resolved
        slug never matches the path -- 403 NOT_PERMITTED, never 404, exactly
        like any other wrong slug on the coded branch."""
        _token, code = public_group_booking_code

        response = _post(anon_client, str(public_group.id), code, _group_booking_payload([]))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json()["error_code"] == "NOT_PERMITTED"
