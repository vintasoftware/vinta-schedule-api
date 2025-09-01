-- PostgreSQL function to calculate recurring blocked time occurrences with bulk modifications and return as JSON
CREATE OR REPLACE FUNCTION get_blocked_time_occurrences_json_with_bulk_modifications(
    p_blocked_time_id BIGINT,
    p_start_date TIMESTAMPTZ,
    p_end_date TIMESTAMPTZ,
    p_max_occurrences INTEGER
)
RETURNS TEXT[] AS $$
DECLARE
    occurrence_row RECORD;
    occurrences TEXT[] := '{}';
BEGIN
    -- Use the enhanced calculate_recurring_blocked_times_with_bulk_modifications function
    FOR occurrence_row IN 
        SELECT 
            occurrence_start,
            occurrence_end,
            is_exception,
            exception_type,
            modified_blocked_time_id,
            source_blocked_time_id
        FROM calculate_recurring_blocked_times_with_bulk_modifications(p_blocked_time_id, p_start_date, p_end_date, p_max_occurrences)
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
                'modified_blocked_time_id', occurrence_row.modified_blocked_time_id,
                'parent_recurring_object_id', p_blocked_time_id,
                'source_blocked_time_id', occurrence_row.source_blocked_time_id
            )::TEXT
        );
    END LOOP;
    
    -- Return the array of JSON strings
    RETURN occurrences;
END;
$$ LANGUAGE plpgsql STABLE;
