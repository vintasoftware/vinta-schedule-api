-- Whether a calendar can take a booking for every range in p_ranges.
--
-- This is the one check of "is this calendar free", for every kind of calendar:
-- crew calendars, calendars that manage their own availability windows, and rooms
-- (resource calendars). It replaces managed_calendar_is_free_in_range, which only
-- covered calendars that manage windows and checked one range.
--
-- True when all of these hold:
--   1. A calendar that manages its windows (manage_available_windows) has, for each
--      range, one window occurrence that covers the whole range. Recurring windows are
--      expanded with calculate_recurring_available_times.
--   2. No event overlaps any range. That is an event on the calendar, or an event on
--      any calendar that holds this one as a room (a resource allocation). Recurring
--      events are expanded with calculate_recurring_events.
--   3. No blocked time on the calendar overlaps any range. Recurring blocked times
--      are expanded with calculate_recurring_blocked_times.
-- Overlap is strict (start < range end AND end > range start), so back-to-back
-- bookings are fine.
--
-- Each recurring row is expanded once, over the span from the first range's start to
-- the last range's end, and its occurrences are matched against every range. The
-- recurrence functions stop at 1000 occurrences per row. A series is only expanded
-- when it can put an occurrence in that span: its rule reaches the span (it starts
-- before the span ends, and its UNTIL is not before the span starts, less the
-- occurrence length, so an occurrence that starts earlier and overlaps the span is
-- still found), or one of its occurrences was moved into the span. A moved
-- occurrence keeps its own time, which can be past UNTIL or before the series'
-- start, and only the series' expansion lists it. A one-off row is found by its own
-- start and end. Each recurring branch filters recurrence_rule_fk_id IS NOT NULL on
-- the row itself, not only through the join to the rule: that lets Postgres drop
-- one-off rows before it calls the recurrence function per row. Without it the
-- function ran for every one-off event as well, about 15 times slower on a room with
-- a few thousand bookings.
--
-- Only base rows count: availability windows and blocked times scoped to an
-- appointment type slot (appointment_type_slot_fk_id IS NOT NULL) are checked
-- elsewhere. Every read is narrowed to p_organization_id.
--
-- A row that replaces one occurrence of a series is told apart by its row in the
-- recurrence exception table (modified_*_fk_id), not by is_recurring_exception. The
-- calendar sync also sets that flag on plain instance rows that have no exception
-- row, and those rows are real and must count. The series' expansion already emits
-- the modified occurrence at its new time, so a linked row is skipped as a one-off
-- row, but only when its series is among the rows expanded here: a moved occurrence
-- can hold a room of its own that its series does not hold, and then only its own
-- row shows it.
--
-- The event rows to look at (on the calendar, or holding it as a room) are a CTE
-- marked NOT MATERIALIZED. It is read more than once, so Postgres would otherwise
-- collect every event the calendar ever had before any time filter applies, which
-- doubled the cost of a one-range check.
--
-- p_ignored_event_ids and p_ignored_blocked_time_ids name rows that do not count as
-- busy. A move passes the event being moved (and, for a moved occurrence, its series)
-- and its own blocked times: they still sit at the old time while the new time is
-- checked, and must not conflict with themselves. An ignored event also hides the
-- occurrence a series' expansion lists for it.
CREATE OR REPLACE FUNCTION calendar_is_free_in_ranges(
    p_organization_id BIGINT,
    p_calendar_id BIGINT,
    p_ranges TSTZRANGE[],
    p_ignored_event_ids BIGINT[] DEFAULT '{}',
    p_ignored_blocked_time_ids BIGINT[] DEFAULT '{}'
)
RETURNS BOOLEAN AS $$
DECLARE
    v_manages_windows BOOLEAN;
    v_start TIMESTAMPTZ;
    v_end TIMESTAMPTZ;
BEGIN
    SELECT min(lower(r)), max(upper(r)) INTO v_start, v_end FROM unnest(p_ranges) r;
    IF v_start IS NULL THEN
        RETURN TRUE;
    END IF;

    SELECT c.manage_available_windows INTO v_manages_windows
    FROM calendar_integration_calendar c
    WHERE c.organization_id = p_organization_id
      AND c.id = p_calendar_id;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    -- 1. A calendar that manages its windows needs one window occurrence covering
    --    each range.
    IF v_manages_windows AND EXISTS (
        WITH window_occurrences AS (
            SELECT av.start_time AS occurrence_start, av.end_time AS occurrence_end
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
              AND av.start_time <= v_end
              AND av.end_time >= v_start
            UNION ALL
            SELECT occ.occurrence_start, occ.occurrence_end
            FROM calendar_integration_availabletime av
            JOIN calendar_integration_recurrencerule rr ON rr.id = av.recurrence_rule_fk_id
            CROSS JOIN LATERAL calculate_recurring_available_times(
                av.id, v_start, v_end, 1000, TRUE
            ) occ
            WHERE av.organization_id = p_organization_id
              AND rr.organization_id = p_organization_id
              AND av.calendar_fk_id = p_calendar_id
              AND av.recurrence_rule_fk_id IS NOT NULL
              AND av.appointment_type_slot_fk_id IS NULL
              AND av.parent_recurring_object_fk_id IS NULL
              AND (
                  (av.start_time <= v_end
                   AND (rr.until IS NULL OR rr.until >= v_start - (av.end_time - av.start_time)))
                  OR EXISTS (
                      SELECT 1
                      FROM calendar_integration_availabletimerecurrenceexception x
                      JOIN calendar_integration_availabletime m
                        ON m.id = x.modified_available_time_fk_id
                        AND m.organization_id = p_organization_id
                      WHERE x.organization_id = p_organization_id
                        AND x.parent_available_time_fk_id = av.id
                        AND NOT x.is_cancelled
                        AND m.start_time < v_end
                        AND m.end_time > v_start
                  )
              )
        )
        SELECT 1
        FROM unnest(p_ranges) r
        WHERE NOT EXISTS (
            SELECT 1
            FROM window_occurrences w
            WHERE w.occurrence_start <= lower(r)
              AND w.occurrence_end >= upper(r)
        )
    ) THEN
        RETURN FALSE;
    END IF;

    -- 2. No event on the calendar, or holding it as a room, may overlap a range.
    IF EXISTS (
        WITH taking_events AS NOT MATERIALIZED (
            SELECT ev.id
            FROM calendar_integration_calendarevent ev
            WHERE ev.organization_id = p_organization_id
              AND ev.calendar_fk_id = p_calendar_id
            UNION
            SELECT ra.event_fk_id
            FROM calendar_integration_resourceallocation ra
            WHERE ra.organization_id = p_organization_id
              AND ra.calendar_fk_id = p_calendar_id
        )
        SELECT 1
        FROM taking_events t
        JOIN calendar_integration_calendarevent ev
          ON ev.id = t.id AND ev.organization_id = p_organization_id
        JOIN unnest(p_ranges) r
          ON ev.start_time < upper(r) AND ev.end_time > lower(r)
        WHERE ev.recurrence_rule_fk_id IS NULL
          AND NOT (ev.id = ANY(p_ignored_event_ids))
          AND ev.start_time < v_end
          AND ev.end_time > v_start
          AND NOT EXISTS (
              SELECT 1
              FROM calendar_integration_eventrecurrenceexception x
              JOIN taking_events p ON p.id = x.parent_event_fk_id
              WHERE x.organization_id = p_organization_id
                AND x.modified_event_fk_id = ev.id
          )
        UNION ALL
        SELECT 1
        FROM taking_events t
        JOIN calendar_integration_calendarevent ev
          ON ev.id = t.id AND ev.organization_id = p_organization_id
        JOIN calendar_integration_recurrencerule rr ON rr.id = ev.recurrence_rule_fk_id
        CROSS JOIN LATERAL calculate_recurring_events(ev.id, v_start, v_end, 1000, TRUE) occ
        JOIN unnest(p_ranges) r
          ON occ.occurrence_start < upper(r) AND occ.occurrence_end > lower(r)
        WHERE rr.organization_id = p_organization_id
          AND ev.recurrence_rule_fk_id IS NOT NULL
          AND ev.parent_recurring_object_fk_id IS NULL
          AND NOT (ev.id = ANY(p_ignored_event_ids))
          AND (
              occ.modified_event_id IS NULL
              OR NOT (occ.modified_event_id = ANY(p_ignored_event_ids))
          )
          AND (
              (ev.start_time < v_end
               AND (rr.until IS NULL OR rr.until >= v_start - (ev.end_time - ev.start_time)))
              OR EXISTS (
                  SELECT 1
                  FROM calendar_integration_eventrecurrenceexception x
                  JOIN calendar_integration_calendarevent m
                    ON m.id = x.modified_event_fk_id AND m.organization_id = p_organization_id
                  WHERE x.organization_id = p_organization_id
                    AND x.parent_event_fk_id = ev.id
                    AND NOT x.is_cancelled
                    AND m.start_time < v_end
                    AND m.end_time > v_start
              )
          )
    ) THEN
        RETURN FALSE;
    END IF;

    -- 3. No blocked time on the calendar may overlap a range.
    IF EXISTS (
        SELECT 1
        FROM calendar_integration_blockedtime bt
        JOIN unnest(p_ranges) r
          ON bt.start_time < upper(r) AND bt.end_time > lower(r)
        WHERE bt.organization_id = p_organization_id
          AND bt.calendar_fk_id = p_calendar_id
          AND bt.appointment_type_slot_fk_id IS NULL
          AND bt.recurrence_rule_fk_id IS NULL
          AND NOT (bt.id = ANY(p_ignored_blocked_time_ids))
          AND bt.start_time < v_end
          AND bt.end_time > v_start
          AND NOT EXISTS (
              SELECT 1
              FROM calendar_integration_blockedtimerecurrenceexception x
              WHERE x.organization_id = p_organization_id
                AND x.modified_blocked_time_fk_id = bt.id
          )
        UNION ALL
        SELECT 1
        FROM calendar_integration_blockedtime bt
        JOIN calendar_integration_recurrencerule rr ON rr.id = bt.recurrence_rule_fk_id
        CROSS JOIN LATERAL calculate_recurring_blocked_times(
            bt.id, v_start, v_end, 1000, TRUE
        ) occ
        JOIN unnest(p_ranges) r
          ON occ.occurrence_start < upper(r) AND occ.occurrence_end > lower(r)
        WHERE bt.organization_id = p_organization_id
          AND rr.organization_id = p_organization_id
          AND bt.calendar_fk_id = p_calendar_id
          AND bt.recurrence_rule_fk_id IS NOT NULL
          AND bt.appointment_type_slot_fk_id IS NULL
          AND bt.parent_recurring_object_fk_id IS NULL
          AND NOT (bt.id = ANY(p_ignored_blocked_time_ids))
          AND (
              (bt.start_time < v_end
               AND (rr.until IS NULL OR rr.until >= v_start - (bt.end_time - bt.start_time)))
              OR EXISTS (
                  SELECT 1
                  FROM calendar_integration_blockedtimerecurrenceexception x
                  JOIN calendar_integration_blockedtime m
                    ON m.id = x.modified_blocked_time_fk_id
                    AND m.organization_id = p_organization_id
                  WHERE x.organization_id = p_organization_id
                    AND x.parent_blocked_time_fk_id = bt.id
                    AND NOT x.is_cancelled
                    AND m.start_time < v_end
                    AND m.end_time > v_start
              )
          )
    ) THEN
        RETURN FALSE;
    END IF;

    RETURN TRUE;
END;
$$ LANGUAGE plpgsql STABLE;
