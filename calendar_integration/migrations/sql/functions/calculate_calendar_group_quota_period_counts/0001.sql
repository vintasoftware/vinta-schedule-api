-- PostgreSQL function to compute, for one calendar inside one CalendarGroupSlot,
-- per-period counts of LIVE bookings made THROUGH that group slot within a
-- search window.
--
-- "Made through the group": the booking has a CalendarEventGroupSelection row
-- for this exact (slot, calendar) pair. Events created directly on the
-- calendar (no CalendarEventGroupSelection) never count.
--
-- "Live": the CalendarEvent row still exists. Cancelling a grouped booking
-- deletes the CalendarEvent (see CalendarGroupService.cancel_grouped_event),
-- which cascades the CalendarEventGroupSelection row -- so a cancelled
-- booking simply has no row to count, and a reschedule (which keeps the same
-- event id, only changing start_time) is counted under whichever period its
-- CURRENT start_time now falls into. No stored quota state; everything is
-- derived on read.
--
-- Period bucketing is done in UTC, not each booking's own local timezone.
-- Neither Calendar, CalendarGroupSlot, nor CalendarGroup carries a canonical
-- IANA timezone in this schema -- only per-row models (CalendarEvent,
-- BlockedTime, AvailableTime) have a `timezone` column, and it is
-- booker-supplied per event. Bucketing on that per-event value would let two
-- live bookings for the same (calendar, slot) in the same real week/day land
-- in different buckets merely by varying the booking's timezone, letting a
-- quota cap be bypassed. So all bookings for a (calendar, slot) are bucketed
-- in ONE consistent frame -- UTC -- regardless of their own `timezone`
-- column: start_time (already a true UTC instant, DST-correct) is truncated
-- via `date_trunc` explicitly `AT TIME ZONE 'UTC'` so the bucketing is
-- deterministic regardless of session timezone, then converted back to a
-- UTC instant for the returned period_start/period_end. Local-timezone
-- alignment (e.g. buckets starting at local midnight) is a deferred
-- enhancement that needs a canonical calendar-level timezone field.
--
-- Week buckets honor p_week_start ('monday' | 'sunday'): Postgres'
-- `date_trunc('week', ...)` always aligns to Monday (ISO 8601), so a
-- Sunday-start week is derived by shifting the timestamp forward one day
-- before truncating to the Monday boundary, then shifting the result back
-- one day -- the Monday of "tomorrow's week" minus one day is today's Sunday
-- (or the most recent Sunday on/before today).
CREATE OR REPLACE FUNCTION calculate_calendar_group_quota_period_counts(
    p_calendar_id BIGINT,
    p_group_slot_id BIGINT,
    p_organization_id BIGINT,
    p_period_type TEXT,
    p_week_start TEXT,
    p_range_start TIMESTAMPTZ,
    p_range_end TIMESTAMPTZ
)
RETURNS TABLE(
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    booking_count INTEGER
) AS $$
BEGIN
    IF p_period_type NOT IN ('day', 'week', 'month') THEN
        RAISE EXCEPTION 'Invalid quota period type: %', p_period_type;
    END IF;

    IF p_period_type = 'week' AND p_week_start NOT IN ('monday', 'sunday') THEN
        RAISE EXCEPTION 'Invalid quota week start: %', p_week_start;
    END IF;

    RETURN QUERY
    WITH live_bookings AS (
        SELECT
            ce.start_time AT TIME ZONE 'UTC' AS utc_start
        FROM calendar_integration_calendareventgroupselection cegs
        INNER JOIN calendar_integration_calendarevent ce
            ON ce.id = cegs.event_fk_id
        WHERE cegs.organization_id = p_organization_id
          AND cegs.slot_fk_id = p_group_slot_id
          AND cegs.calendar_fk_id = p_calendar_id
          AND ce.organization_id = p_organization_id
          AND ce.start_time >= p_range_start
          AND ce.start_time < p_range_end
    ),
    bucketed AS (
        SELECT
            CASE p_period_type
                WHEN 'day' THEN date_trunc('day', utc_start)
                WHEN 'month' THEN date_trunc('month', utc_start)
                ELSE (
                    CASE WHEN p_week_start = 'sunday'
                        THEN date_trunc('week', utc_start + INTERVAL '1 day') - INTERVAL '1 day'
                        ELSE date_trunc('week', utc_start)
                    END
                )
            END AS local_period_start
        FROM live_bookings
    )
    SELECT
        (b.local_period_start AT TIME ZONE 'UTC') AS period_start,
        (
            CASE p_period_type
                WHEN 'day' THEN b.local_period_start + INTERVAL '1 day'
                WHEN 'month' THEN b.local_period_start + INTERVAL '1 month'
                ELSE b.local_period_start + INTERVAL '1 week'
            END
        ) AT TIME ZONE 'UTC' AS period_end,
        COUNT(*)::INTEGER AS booking_count
    FROM bucketed b
    GROUP BY b.local_period_start
    ORDER BY period_start;
END;
$$ LANGUAGE plpgsql;
