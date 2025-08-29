-- PostgreSQL function to calculate recurring event occurrences and return as JSON
-- This allows Django ORM to use the result in annotations and filters
CREATE OR REPLACE FUNCTION get_event_occurrences_json(
    p_event_id BIGINT,
    p_start_date TIMESTAMPTZ,
    p_end_date TIMESTAMPTZ,
    p_max_occurrences INTEGER
)
RETURNS TEXT[] AS $$
DECLARE
    occurrence_row RECORD;
    occurrences TEXT[] := '{}';
BEGIN
    -- Use the existing calculate_recurring_events function
    FOR occurrence_row IN 
        SELECT 
            occurrence_start,
            occurrence_end,
            is_exception,
            exception_type,
            modified_event_id
        FROM calculate_recurring_events(p_event_id, p_start_date, p_end_date, p_max_occurrences)
        ORDER BY occurrence_start
    LOOP
        -- Build JSON object for each occurrence as text
        occurrences := array_append(
            occurrences,
            json_build_object(
                'start_time', occurrence_row.occurrence_start,
                'end_time', occurrence_row.occurrence_end,
                'is_exception', occurrence_row.is_exception,
                'exception_type', occurrence_row.exception_type,
                'modified_event_id', occurrence_row.modified_event_id,
                'parent_recurring_object_id', p_event_id
            )::TEXT
        );
    END LOOP;
    
    -- Return the array of JSON strings
    RETURN occurrences;
END;
$$ LANGUAGE plpgsql STABLE;