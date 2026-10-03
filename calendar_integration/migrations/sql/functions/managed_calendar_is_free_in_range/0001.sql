-- Whether a calendar that manages its own availability windows can take a booking
-- for [p_start, p_end).
--
-- True when both hold:
--   1. An availability window occurrence fully covers the range. Recurring windows
--      are expanded with calculate_recurring_available_times, so every occurrence
--      counts, not only the first one stored on the master row.
--   2. No event and no blocked time occurrence overlaps the range. Overlap is
--      strict (start < p_end AND end > p_start), so back-to-back bookings are fine.
--
-- Only base rows count: availability windows and blocked times scoped to an
-- appointment type slot (appointment_type_slot_fk_id IS NOT NULL) are checked
-- elsewhere. Every read is narrowed to p_organization_id.
--
-- A row that replaces one occurrence of a series is told apart by its row in the
-- recurrence exception table (modified_*_fk_id), not by is_recurring_exception.
-- The calendar sync also sets that flag on plain instance rows that have no exception
-- row, and those rows are real and must count. The expansion already emits the
-- modified occurrence at its new time, so linked rows are skipped here.
--
-- A recurring series is only expanded when its rule can still reach the range:
-- UNTIL must not end before the range start (less the occurrence length, so an
-- occurrence that starts earlier and overlaps the range is still found).
CREATE OR REPLACE FUNCTION managed_calendar_is_free_in_range(
    p_organization_id BIGINT,
    p_calendar_id BIGINT,
    p_start TIMESTAMPTZ,
    p_end TIMESTAMPTZ
)
RETURNS BOOLEAN AS $$
BEGIN
    -- 1. A window occurrence must cover the whole range.
    IF NOT (
        EXISTS (
            SELECT 1
            FROM calendar_integration_availabletime av
            WHERE av.organization_id = p_organization_id
              AND av.calendar_fk_id = p_calendar_id
              AND av.appointment_type_slot_fk_id IS NULL
              AND av.recurrence_rule_fk_id IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM calendar_integration_availabletimerecurrenceexception x
                  WHERE x.organization_id = p_organization_id
                    AND x.modified_available_time_fk_id = av.id
              )
              AND av.start_time <= p_start
              AND av.end_time >= p_end
        )
        OR EXISTS (
            SELECT 1
            FROM calendar_integration_availabletime av
            JOIN calendar_integration_recurrencerule rr ON rr.id = av.recurrence_rule_fk_id
            CROSS JOIN LATERAL calculate_recurring_available_times(
                av.id, p_start, p_end, 1000, TRUE
            ) occ
            WHERE av.organization_id = p_organization_id
              AND rr.organization_id = p_organization_id
              AND av.calendar_fk_id = p_calendar_id
              AND av.appointment_type_slot_fk_id IS NULL
              AND av.parent_recurring_object_fk_id IS NULL
              AND av.start_time <= p_end
              AND (rr.until IS NULL OR rr.until >= p_start - (av.end_time - av.start_time))
              AND occ.occurrence_start <= p_start
              AND occ.occurrence_end >= p_end
        )
    ) THEN
        RETURN FALSE;
    END IF;

    -- 2a. No event may overlap the range.
    IF EXISTS (
        SELECT 1
        FROM calendar_integration_calendarevent ev
        WHERE ev.organization_id = p_organization_id
          AND ev.calendar_fk_id = p_calendar_id
          AND ev.recurrence_rule_fk_id IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM calendar_integration_eventrecurrenceexception x
              WHERE x.organization_id = p_organization_id
                AND x.modified_event_fk_id = ev.id
          )
          AND ev.start_time < p_end
          AND ev.end_time > p_start
    ) OR EXISTS (
        SELECT 1
        FROM calendar_integration_calendarevent ev
        JOIN calendar_integration_recurrencerule rr ON rr.id = ev.recurrence_rule_fk_id
        CROSS JOIN LATERAL calculate_recurring_events(ev.id, p_start, p_end, 1000, TRUE) occ
        WHERE ev.organization_id = p_organization_id
          AND rr.organization_id = p_organization_id
          AND ev.calendar_fk_id = p_calendar_id
          AND ev.parent_recurring_object_fk_id IS NULL
          AND ev.start_time < p_end
          AND (rr.until IS NULL OR rr.until >= p_start - (ev.end_time - ev.start_time))
          AND occ.occurrence_start < p_end
          AND occ.occurrence_end > p_start
    ) THEN
        RETURN FALSE;
    END IF;

    -- 2b. No blocked time may overlap the range.
    IF EXISTS (
        SELECT 1
        FROM calendar_integration_blockedtime bt
        WHERE bt.organization_id = p_organization_id
          AND bt.calendar_fk_id = p_calendar_id
          AND bt.appointment_type_slot_fk_id IS NULL
          AND bt.recurrence_rule_fk_id IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM calendar_integration_blockedtimerecurrenceexception x
              WHERE x.organization_id = p_organization_id
                AND x.modified_blocked_time_fk_id = bt.id
          )
          AND bt.start_time < p_end
          AND bt.end_time > p_start
    ) OR EXISTS (
        SELECT 1
        FROM calendar_integration_blockedtime bt
        JOIN calendar_integration_recurrencerule rr ON rr.id = bt.recurrence_rule_fk_id
        CROSS JOIN LATERAL calculate_recurring_blocked_times(bt.id, p_start, p_end, 1000, TRUE) occ
        WHERE bt.organization_id = p_organization_id
          AND rr.organization_id = p_organization_id
          AND bt.calendar_fk_id = p_calendar_id
          AND bt.appointment_type_slot_fk_id IS NULL
          AND bt.parent_recurring_object_fk_id IS NULL
          AND bt.start_time < p_end
          AND (rr.until IS NULL OR rr.until >= p_start - (bt.end_time - bt.start_time))
          AND occ.occurrence_start < p_end
          AND occ.occurrence_end > p_start
    ) THEN
        RETURN FALSE;
    END IF;

    RETURN TRUE;
END;
$$ LANGUAGE plpgsql STABLE;
