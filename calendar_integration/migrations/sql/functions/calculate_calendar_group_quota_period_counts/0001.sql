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
-- Period bucketing is aligned to calendar boundaries in the booking's own
-- local timezone (CalendarEvent.timezone), not UTC: start_time (a true UTC
-- instant, DST-correct) is converted to that local wall-clock time via
-- `AT TIME ZONE`, bucketed there, then converted back to a UTC instant for
-- the returned period_start/period_end.
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
            ce.timezone AS event_timezone,
            ce.start_time AT TIME ZONE ce.timezone AS local_start
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
            event_timezone,
            CASE p_period_type
                WHEN 'day' THEN date_trunc('day', local_start)
                WHEN 'month' THEN date_trunc('month', local_start)
                ELSE (
                    CASE WHEN p_week_start = 'sunday'
                        THEN date_trunc('week', local_start + INTERVAL '1 day') - INTERVAL '1 day'
                        ELSE date_trunc('week', local_start)
                    END
                )
            END AS local_period_start
        FROM live_bookings
    )
    SELECT
        (b.local_period_start AT TIME ZONE b.event_timezone) AS period_start,
        (
            CASE p_period_type
                WHEN 'day' THEN b.local_period_start + INTERVAL '1 day'
                WHEN 'month' THEN b.local_period_start + INTERVAL '1 month'
                ELSE b.local_period_start + INTERVAL '1 week'
            END
        ) AT TIME ZONE b.event_timezone AS period_end,
        COUNT(*)::INTEGER AS booking_count
    FROM bucketed b
    GROUP BY b.event_timezone, b.local_period_start
    ORDER BY period_start;
END;
$$ LANGUAGE plpgsql;
