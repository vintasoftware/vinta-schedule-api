"""``appointmentTypeAvailability`` over an organization token: the ranges it accepts.

Each range must end after it starts. A range that does not is refused with a GraphQL
error before availability is computed, as the code-gated variant already does.
"""

import pytest


pytestmark = pytest.mark.django_db

APPOINTMENT_TYPE_AVAILABILITY_QUERY = """
query AppointmentTypeAvailability($appointmentTypeId: Int!, $ranges: [DateTimeRangeInput!]!) {
    appointmentTypeAvailability(appointmentTypeId: $appointmentTypeId, ranges: $ranges) {
        startTime
        slots { slotId availableCalendarIds }
    }
}
"""


def _availability(post, appointment_type_id: int, start: str, end: str) -> dict:
    return post(
        APPOINTMENT_TYPE_AVAILABILITY_QUERY,
        {
            "appointmentTypeId": appointment_type_id,
            "ranges": [{"startTime": start, "endTime": end}],
        },
    )


def test_a_range_inside_the_open_window_lists_the_calendar(
    org_wide_post, appointment_type_with_slot, booking_calendar
):
    appointment_type, _slot = appointment_type_with_slot

    result = _availability(
        org_wide_post,
        appointment_type.id,
        "2030-06-01T10:00:00+00:00",
        "2030-06-01T10:30:00+00:00",
    )

    assert result.get("errors") is None
    [range_result] = result["data"]["appointmentTypeAvailability"]
    assert [s["availableCalendarIds"] for s in range_result["slots"]] == [[booking_calendar.id]]


@pytest.mark.parametrize(
    "end", ["2030-06-01T10:00:00+00:00", "2030-06-01T09:00:00+00:00"], ids=["empty", "reversed"]
)
def test_a_range_that_does_not_move_forward_is_refused(
    org_wide_post, appointment_type_with_slot, end
):
    appointment_type, _slot = appointment_type_with_slot

    result = _availability(org_wide_post, appointment_type.id, "2030-06-01T10:00:00+00:00", end)

    assert [e["message"] for e in result["errors"]] == ["Invalid time range."]
