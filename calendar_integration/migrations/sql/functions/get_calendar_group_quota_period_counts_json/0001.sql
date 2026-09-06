-- PostgreSQL function to compute per-period LIVE booking counts for one
-- calendar inside one CalendarGroupSlot, returned as a JSON-per-element TEXT
-- array so Django ORM can consume it through a Func annotation (mirrors
-- get_event_occurrences_json wrapping calculate_recurring_events).
CREATE OR REPLACE FUNCTION get_calendar_group_quota_period_counts_json(
    p_calendar_id BIGINT,
    p_group_slot_id BIGINT,
    p_organization_id BIGINT,
    p_period_type TEXT,
    p_week_start TEXT,
    p_range_start TIMESTAMPTZ,
    p_range_end TIMESTAMPTZ
)
RETURNS TEXT[] AS $$
DECLARE
    bucket_row RECORD;
    counts TEXT[] := '{}';
BEGIN
    FOR bucket_row IN
        SELECT period_start, period_end, booking_count
        FROM calculate_calendar_group_quota_period_counts(
            p_calendar_id,
            p_group_slot_id,
            p_organization_id,
            p_period_type,
            p_week_start,
            p_range_start,
            p_range_end
        )
        ORDER BY period_start
    LOOP
        counts := array_append(
            counts,
            json_build_object(
                'period_start', bucket_row.period_start,
                'period_end', bucket_row.period_end,
                'booking_count', bucket_row.booking_count
            )::TEXT
        );
    END LOOP;

    RETURN counts;
END;
$$ LANGUAGE plpgsql STABLE;
