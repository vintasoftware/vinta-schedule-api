-- Enhanced function to get available time occurrences with bulk modification support
CREATE OR REPLACE FUNCTION get_available_time_occurrences_with_bulk_modifications_json(
    p_available_time_id BIGINT,
    p_start_date TIMESTAMPTZ,
    p_end_date TIMESTAMPTZ,
    p_max_occurrences INTEGER
)
RETURNS TEXT[] AS $$
DECLARE
    occurrence_row RECORD;
    bulk_mod_row RECORD;
    occurrences TEXT[] := '{}';
    original_available_time calendar_integration_availabletime%ROWTYPE;
BEGIN
    -- Get the original available time
    SELECT * INTO original_available_time 
    FROM calendar_integration_availabletime 
    WHERE id = p_available_time_id;
    
    IF NOT FOUND THEN
        RETURN occurrences;
    END IF;
    
    -- Get occurrences from the original (potentially truncated) available time
    FOR occurrence_row IN 
        SELECT 
            occurrence_start,
            occurrence_end,
            is_exception,
            exception_type,
            modified_available_time_id
        FROM calculate_recurring_available_times(p_available_time_id, p_start_date, p_end_date, p_max_occurrences)
        ORDER BY occurrence_start
    LOOP
        occurrences := array_append(
            occurrences,
            json_build_object(
                'start_time', occurrence_row.occurrence_start,
                'end_time', occurrence_row.occurrence_end,
                'is_exception', occurrence_row.is_exception,
                'exception_type', occurrence_row.exception_type,
                'modified_available_time_id', occurrence_row.modified_available_time_id,
                'parent_recurring_object_id', p_available_time_id,
                'is_bulk_continuation', false
            )::TEXT
        );
    END LOOP;
    
    -- Get occurrences from bulk modification continuations
    FOR bulk_mod_row IN
        SELECT id as continuation_available_time_id
        FROM calendar_integration_availabletime continuation
        WHERE continuation.bulk_modification_parent_fk_id = p_available_time_id
            AND continuation.organization_id = original_available_time.organization_id
    LOOP
        FOR occurrence_row IN 
            SELECT 
                occurrence_start,
                occurrence_end,
                is_exception,
                exception_type,
                modified_available_time_id
            FROM calculate_recurring_available_times(
                bulk_mod_row.continuation_available_time_id, 
                p_start_date, 
                p_end_date, 
                p_max_occurrences
            )
            ORDER BY occurrence_start
        LOOP
            occurrences := array_append(
                occurrences,
                json_build_object(
                    'start_time', occurrence_row.occurrence_start,
                    'end_time', occurrence_row.occurrence_end,
                    'is_exception', occurrence_row.is_exception,
                    'exception_type', occurrence_row.exception_type,
                    'modified_available_time_id', occurrence_row.modified_available_time_id,
                    'parent_recurring_object_id', bulk_mod_row.continuation_available_time_id,
                    'is_bulk_continuation', true,
                    'bulk_modification_root_id', p_available_time_id
                )::TEXT
            );
        END LOOP;
    END LOOP;
    
    RETURN occurrences;
END;
$$ LANGUAGE plpgsql STABLE;