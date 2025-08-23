-- Alternative CTE-based approach for simpler cases (daily/weekly without complex BY* rules)
CREATE OR REPLACE FUNCTION calculate_recurring_events_simple(
    p_event_id BIGINT,
    p_start_date TIMESTAMPTZ,
    p_end_date TIMESTAMPTZ,
    p_max_occurrences INTEGER
)
RETURNS TABLE(
    occ_start TIMESTAMPTZ,
    occ_end TIMESTAMPTZ,
    is_exception BOOLEAN,
    exception_type TEXT,
    modified_event_id BIGINT,
    parent_event_id BIGINT
) AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE event_data AS (
        SELECT 
            e.id,
            e.start_time,
            e.end_time,
            e.end_time - e.start_time as duration,
            r.frequency,
            r.interval,
            r.count,
            r.until,
            r.by_weekday
        FROM calendar_integration_calendarevent e
        LEFT JOIN calendar_integration_recurrencerule r ON e.recurrence_rule_fk_id = r.id
        WHERE e.id = p_event_id
    ),
    
    occurrence_generator AS (
        -- Base case: first occurrence
        SELECT 
            ed.start_time as occurrence_start,
            ed.end_time as occurrence_end,
            ed.duration,
            ed.frequency,
            ed.interval,
            ed.count,
            ed.until,
            ed.by_weekday,
            ed.id as parent_event_id,
            1 as occurrence_num
        FROM event_data ed
        WHERE ed.frequency IS NOT NULL -- Only for recurring events
        
        UNION ALL
        
        -- Recursive case: generate next occurrences
        SELECT 
            CASE 
                WHEN og.frequency = 'DAILY' THEN 
                    og.occurrence_start + (og.interval || ' days')::interval
                WHEN og.frequency = 'WEEKLY' AND og.by_weekday IS NULL THEN 
                    og.occurrence_start + (og.interval * 7 || ' days')::interval
                WHEN og.frequency = 'MONTHLY' THEN 
                    og.occurrence_start + (og.interval || ' months')::interval
                WHEN og.frequency = 'YEARLY' THEN 
                    og.occurrence_start + (og.interval || ' years')::interval
                ELSE og.occurrence_start + interval '1 day' -- fallback
            END as occurrence_start,
            CASE 
                WHEN og.frequency = 'DAILY' THEN 
                    og.occurrence_start + (og.interval || ' days')::interval + og.duration
                WHEN og.frequency = 'WEEKLY' AND og.by_weekday IS NULL THEN 
                    og.occurrence_start + (og.interval * 7 || ' days')::interval + og.duration
                WHEN og.frequency = 'MONTHLY' THEN 
                    og.occurrence_start + (og.interval || ' months')::interval + og.duration
                WHEN og.frequency = 'YEARLY' THEN 
                    og.occurrence_start + (og.interval || ' years')::interval + og.duration
                ELSE og.occurrence_start + interval '1 day' + og.duration
            END as occurrence_end,
            og.duration,
            og.frequency,
            og.interval,
            og.count,
            og.until,
            og.by_weekday,
            COALESCE(og.parent_event_id, NULL) AS parent_event_id,
            og.occurrence_num + 1
        FROM occurrence_generator og
        WHERE 
            og.occurrence_start <= p_end_date
            AND (og.until IS NULL OR og.occurrence_start <= og.until)
            AND (og.count IS NULL OR og.occurrence_num < og.count)
            AND og.occurrence_num < LEAST(p_max_occurrences,10000) -- Safety limit
    ),
    
    filtered_occurrences AS (
        SELECT 
            occurrence_start,
            occurrence_end,
            parent_event_id
        FROM occurrence_generator
        WHERE occurrence_start >= p_start_date 
        AND occurrence_start <= p_end_date
        AND NOT EXISTS (
            SELECT 1 FROM calendar_integration_recurrenceexception re
            WHERE re.parent_event_fk_id = p_event_id 
            AND re.exception_date = occurrence_start
        )
    )
    
    -- Return regular occurrences
    SELECT 
        fo.occurrence_start as occ_start,
        fo.occurrence_end as occ_end,
        FALSE as is_exception,
        NULL::TEXT as exception_type,
        NULL::INTEGER as modified_event_id,
        ed.id as parent_event_id
    FROM filtered_occurrences fo
    
    UNION ALL
    
    -- Return non-recurring event if it falls in range
    SELECT 
        ed.start_time as occ_start,
        ed.end_time as occ_end,
        FALSE as is_exception,
        NULL::TEXT as exception_type,
        NULL::INTEGER as modified_event_id,
        ed.id as parent_event_id
    FROM event_data ed
    WHERE ed.frequency IS NULL 
    AND ed.start_time >= p_start_date 
    AND ed.start_time <= p_end_date
    
    UNION ALL
    
    -- Return modified exceptions
    SELECT 
        me.start_time as occ_start,
        me.end_time as occ_end,
        TRUE as is_exception,
        'modified' as exception_type,
        me.id as modified_event_id,
        re.parent_event_fk_id as parent_event_id
    FROM calendar_integration_recurrenceexception re
    JOIN calendar_integration_calendarevent me ON re.modified_event_fk_id = me.id
    WHERE re.parent_event_fk_id = p_event_id
    AND NOT re.is_cancelled
    AND me.start_time >= p_start_date 
    AND me.start_time <= p_end_date
    
    ORDER BY occ_start;
END;
$$ LANGUAGE plpgsql;
