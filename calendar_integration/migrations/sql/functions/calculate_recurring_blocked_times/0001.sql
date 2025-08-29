-- PostgreSQL function to calculate recurring blocked time occurrences
-- Simplified version based on calculate_recurring_events
CREATE OR REPLACE FUNCTION calculate_recurring_blocked_times(
    p_blocked_time_id BIGINT,
    p_start_date TIMESTAMPTZ,
    p_end_date TIMESTAMPTZ,
    p_max_occurrences INTEGER
)
RETURNS TABLE(
    occurrence_start TIMESTAMPTZ,
    occurrence_end TIMESTAMPTZ,
    is_exception BOOLEAN,
    exception_type VARCHAR(20),
    modified_blocked_time_id BIGINT
) AS $$
DECLARE
    v_blocked_time RECORD;
    v_rule RECORD;
    v_duration INTERVAL;
    v_current_date TIMESTAMPTZ;
    v_count INTEGER := 0;
    v_max_count INTEGER := COALESCE(p_max_occurrences, 1000);
    
BEGIN
    -- Get blocked time details
    SELECT * INTO v_blocked_time 
    FROM calendar_integration_blockedtime 
    WHERE id = p_blocked_time_id;
    
    IF NOT FOUND THEN
        RETURN;
    END IF;
    
    -- Check if blocked time is recurring
    IF v_blocked_time.recurrence_rule_fk_id IS NULL THEN
        -- Non-recurring blocked time, just check if it falls within range
        IF v_blocked_time.start_time >= p_start_date AND v_blocked_time.start_time <= p_end_date THEN
            occurrence_start := v_blocked_time.start_time;
            occurrence_end := v_blocked_time.end_time;
            is_exception := FALSE;
            exception_type := NULL;
            modified_blocked_time_id := NULL;
            RETURN NEXT;
        END IF;
        RETURN;
    END IF;
    
    -- Get the recurrence rule
    SELECT * INTO v_rule 
    FROM calendar_integration_recurrencerule 
    WHERE id = v_blocked_time.recurrence_rule_fk_id;
    
    IF NOT FOUND THEN
        RETURN;
    END IF;
    
    -- Calculate blocked time duration
    v_duration := v_blocked_time.end_time - v_blocked_time.start_time;
    
    -- Start from blocked time's start time
    v_current_date := v_blocked_time.start_time;
    
    -- Simple implementation: generate occurrences based on frequency
    WHILE v_current_date <= p_end_date AND v_count < v_max_count LOOP
        -- Check termination conditions
        IF v_rule.until IS NOT NULL AND v_current_date > v_rule.until THEN
            EXIT;
        END IF;
        
        IF v_rule.count IS NOT NULL AND v_count >= v_rule.count THEN
            EXIT;
        END IF;
        
        -- Check if occurrence is within date range
        IF v_current_date >= p_start_date AND v_current_date <= p_end_date THEN
            -- For now, return basic occurrence (exception handling can be added later)
            occurrence_start := v_current_date;
            occurrence_end := v_current_date + v_duration;
            is_exception := FALSE;
            exception_type := NULL;
            modified_blocked_time_id := NULL;
            RETURN NEXT;
            v_count := v_count + 1;
        END IF;
        
        -- Calculate next occurrence based on frequency
        CASE v_rule.frequency
            WHEN 'DAILY' THEN
                v_current_date := v_current_date + (COALESCE(v_rule.interval, 1) || ' days')::INTERVAL;
            WHEN 'WEEKLY' THEN
                v_current_date := v_current_date + (COALESCE(v_rule.interval, 1) * 7 || ' days')::INTERVAL;
            WHEN 'MONTHLY' THEN
                v_current_date := v_current_date + (COALESCE(v_rule.interval, 1) || ' months')::INTERVAL;
            WHEN 'YEARLY' THEN
                v_current_date := v_current_date + (COALESCE(v_rule.interval, 1) || ' years')::INTERVAL;
            ELSE
                EXIT; -- Unknown frequency
        END CASE;
    END LOOP;
    
    RETURN;
END;
$$ LANGUAGE plpgsql STABLE;
