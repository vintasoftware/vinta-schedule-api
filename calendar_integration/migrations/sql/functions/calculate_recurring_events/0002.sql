-- Enhanced PostgreSQL function to calculate recurring event occurrences with bulk modification support
-- This function automatically includes continuation objects created by bulk modifications

CREATE OR REPLACE FUNCTION calculate_recurring_events_with_bulk_modifications(
    p_event_id BIGINT,
    p_start_date TIMESTAMPTZ,
    p_end_date TIMESTAMPTZ,
    p_max_occurrences INTEGER
)
RETURNS TABLE(
    occurrence_start TIMESTAMPTZ,
    occurrence_end TIMESTAMPTZ,
    is_exception BOOLEAN,
    exception_type TEXT,
    modified_event_id BIGINT,
    source_event_id BIGINT
) AS $$
DECLARE
    v_event calendar_integration_calendarevent%ROWTYPE;
    v_continuation_event calendar_integration_calendarevent%ROWTYPE;
    v_occurrence_count INTEGER := 0;
    continuation_cursor CURSOR FOR 
        SELECT * FROM calendar_integration_calendarevent 
        WHERE bulk_modification_parent_fk_id = p_event_id 
        AND organization_id = (SELECT organization_id FROM calendar_integration_calendarevent WHERE id = p_event_id);
BEGIN
    -- Get the main event details
    SELECT * INTO v_event 
    FROM calendar_integration_calendarevent 
    WHERE id = p_event_id;
    
    IF NOT FOUND THEN
        RETURN;
    END IF;
    
    -- First, get occurrences from the original event (potentially truncated)
    FOR occurrence_start, occurrence_end, is_exception, exception_type, modified_event_id IN
        SELECT o.occurrence_start, o.occurrence_end, o.is_exception, o.exception_type, o.modified_event_id
        FROM calculate_recurring_events(p_event_id, p_start_date, p_end_date, p_max_occurrences) o
    LOOP
        source_event_id := p_event_id;
        v_occurrence_count := v_occurrence_count + 1;
        RETURN NEXT;
        
        -- Exit if we've reached the max occurrences limit
        IF v_occurrence_count >= p_max_occurrences THEN
            RETURN;
        END IF;
    END LOOP;
    
    -- Then, get occurrences from any continuation events (bulk modification children)
    OPEN continuation_cursor;
    LOOP
        FETCH continuation_cursor INTO v_continuation_event;
        EXIT WHEN NOT FOUND;
        
        -- Get occurrences from this continuation event
        FOR occurrence_start, occurrence_end, is_exception, exception_type, modified_event_id IN
            SELECT o.occurrence_start, o.occurrence_end, o.is_exception, o.exception_type, o.modified_event_id
            FROM calculate_recurring_events(v_continuation_event.id, p_start_date, p_end_date, p_max_occurrences - v_occurrence_count) o
        LOOP
            source_event_id := v_continuation_event.id;
            v_occurrence_count := v_occurrence_count + 1;
            RETURN NEXT;
            
            -- Exit if we've reached the max occurrences limit
            IF v_occurrence_count >= p_max_occurrences THEN
                CLOSE continuation_cursor;
                RETURN;
            END IF;
        END LOOP;
        
        -- Exit if we've reached the max occurrences limit
        IF v_occurrence_count >= p_max_occurrences THEN
            EXIT;
        END IF;
    END LOOP;
    CLOSE continuation_cursor;
    
END;
$$ LANGUAGE plpgsql STABLE;
