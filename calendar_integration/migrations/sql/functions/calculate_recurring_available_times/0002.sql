-- Enhanced PostgreSQL function to calculate recurring available time occurrences with bulk modification support

CREATE OR REPLACE FUNCTION calculate_recurring_available_times_with_bulk_modifications(
    p_available_time_id BIGINT,
    p_start_date TIMESTAMPTZ,
    p_end_date TIMESTAMPTZ,
    p_max_occurrences INTEGER
)
RETURNS TABLE(
    occurrence_start TIMESTAMPTZ,
    occurrence_end TIMESTAMPTZ,
    is_exception BOOLEAN,
    exception_type TEXT,
    modified_available_time_id BIGINT,
    source_available_time_id BIGINT
) AS $$
DECLARE
    v_available_time calendar_integration_availabletime%ROWTYPE;
    v_continuation_available_time calendar_integration_availabletime%ROWTYPE;
    v_occurrence_count INTEGER := 0;
    continuation_cursor CURSOR FOR 
        SELECT * FROM calendar_integration_availabletime 
        WHERE bulk_modification_parent_fk_id = p_available_time_id 
        AND organization_id = (SELECT organization_id FROM calendar_integration_availabletime WHERE id = p_available_time_id);
BEGIN
    -- Get the main available time details
    SELECT * INTO v_available_time 
    FROM calendar_integration_availabletime 
    WHERE id = p_available_time_id;
    
    IF NOT FOUND THEN
        RETURN;
    END IF;
    
    -- First, get occurrences from the original available time (potentially truncated)
    FOR occurrence_start, occurrence_end, is_exception, exception_type, modified_available_time_id IN
        SELECT o.occurrence_start, o.occurrence_end, o.is_exception, o.exception_type, o.modified_available_time_id
        FROM calculate_recurring_available_times(p_available_time_id, p_start_date, p_end_date, p_max_occurrences) o
    LOOP
        source_available_time_id := p_available_time_id;
        v_occurrence_count := v_occurrence_count + 1;
        RETURN NEXT;
        
        -- Exit if we've reached the max occurrences limit
        IF v_occurrence_count >= p_max_occurrences THEN
            RETURN;
        END IF;
    END LOOP;
    
    -- Then, get occurrences from any continuation available times (bulk modification children)
    OPEN continuation_cursor;
    LOOP
        FETCH continuation_cursor INTO v_continuation_available_time;
        EXIT WHEN NOT FOUND;
        
        -- Get occurrences from this continuation available time
        FOR occurrence_start, occurrence_end, is_exception, exception_type, modified_available_time_id IN
            SELECT o.occurrence_start, o.occurrence_end, o.is_exception, o.exception_type, o.modified_available_time_id
            FROM calculate_recurring_available_times(v_continuation_available_time.id, p_start_date, p_end_date, p_max_occurrences - v_occurrence_count) o
        LOOP
            source_available_time_id := v_continuation_available_time.id;
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
