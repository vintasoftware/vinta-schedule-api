-- PostgreSQL function to calculate recurring event occurrences within a date range
-- This function handles all RecurrenceRule configurations and considers RecurrenceExceptions

CREATE OR REPLACE FUNCTION calculate_recurring_events(
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
    modified_event_id BIGINT
) AS $$
DECLARE
    v_event calendar_integration_calendarevent%ROWTYPE;
    v_rule calendar_integration_recurrencerule%ROWTYPE;
    v_current_date TIMESTAMPTZ;
    v_duration INTERVAL;
    v_occurrence_count INTEGER := 0;
    v_total_occurrences INTEGER := 0;
    v_temp_occurrence_count INTEGER := 0;
    v_max_occurrences INTEGER := LEAST(p_max_occurrences, 1000); -- Safety limit
    v_weekday_map INTEGER[] := ARRAY[1,2,3,4,5,6,0]; -- MO,TU,WE,TH,FR,SA,SU -> 1,2,3,4,5,6,0
    v_weekdays INTEGER[];
    v_month_days INTEGER[];
    v_months INTEGER[];
    v_year_days INTEGER[];
    v_week_numbers INTEGER[];
    v_hours INTEGER[];
    v_minutes INTEGER[];
    v_seconds INTEGER[];
    v_week_start INTEGER;
    v_target_weekday INTEGER;
    v_days_ahead INTEGER;
    v_found_in_week BOOLEAN;
    v_temp_date TIMESTAMPTZ;
    v_exception_exists BOOLEAN;
BEGIN
    -- Get the event details
    SELECT * INTO v_event 
    FROM calendar_integration_calendarevent 
    WHERE id = p_event_id;
    
    IF NOT FOUND THEN
        RETURN;
    END IF;
    
    -- Check if event is recurring
    IF v_event.recurrence_rule_fk_id IS NULL THEN
        -- Non-recurring event, just check if it falls within range
        IF v_event.start_time >= p_start_date AND v_event.start_time <= p_end_date THEN
            occurrence_start := v_event.start_time;
            occurrence_end := v_event.end_time;
            is_exception := FALSE;
            exception_type := NULL;
            modified_event_id := NULL;
            RETURN NEXT;
        END IF;
        RETURN;
    END IF;
    
    -- Get the recurrence rule
    SELECT * INTO v_rule 
    FROM calendar_integration_recurrencerule 
    WHERE id = v_event.recurrence_rule_fk_id;
    
    IF NOT FOUND THEN
        RETURN;
    END IF;
    
    -- Calculate event duration
    v_duration := v_event.end_time - v_event.start_time;
    
    -- Parse BY* fields into arrays
    IF v_rule.by_weekday IS NOT NULL AND v_rule.by_weekday != '' THEN
        v_weekdays := ARRAY(
            SELECT CASE 
                WHEN trim(unnest) = 'MO' THEN 1
                WHEN trim(unnest) = 'TU' THEN 2
                WHEN trim(unnest) = 'WE' THEN 3
                WHEN trim(unnest) = 'TH' THEN 4
                WHEN trim(unnest) = 'FR' THEN 5
                WHEN trim(unnest) = 'SA' THEN 6
                WHEN trim(unnest) = 'SU' THEN 0
                ELSE NULL
            END
            FROM unnest(string_to_array(v_rule.by_weekday, ','))
            WHERE trim(unnest) IN ('MO','TU','WE','TH','FR','SA','SU')
        );
    END IF;
    
    IF v_rule.by_month_day IS NOT NULL AND v_rule.by_month_day != '' THEN
        v_month_days := ARRAY(
            SELECT CAST(trim(unnest) AS INTEGER)
            FROM unnest(string_to_array(v_rule.by_month_day, ','))
            WHERE trim(unnest) ~ '^-?[0-9]+$'
        );
    END IF;
    
    IF v_rule.by_month IS NOT NULL AND v_rule.by_month != '' THEN
        v_months := ARRAY(
            SELECT CAST(trim(unnest) AS INTEGER)
            FROM unnest(string_to_array(v_rule.by_month, ','))
            WHERE trim(unnest) ~ '^[0-9]+$'
        );
    END IF;
    
    IF v_rule.by_year_day IS NOT NULL AND v_rule.by_year_day != '' THEN
        v_year_days := ARRAY(
            SELECT CAST(trim(unnest) AS INTEGER)
            FROM unnest(string_to_array(v_rule.by_year_day, ','))
            WHERE trim(unnest) ~ '^-?[0-9]+$'
        );
    END IF;
    
    IF v_rule.by_week_number IS NOT NULL AND v_rule.by_week_number != '' THEN
        v_week_numbers := ARRAY(
            SELECT CAST(trim(unnest) AS INTEGER)
            FROM unnest(string_to_array(v_rule.by_week_number, ','))
            WHERE trim(unnest) ~ '^-?[0-9]+$'
        );
    END IF;
    
    IF v_rule.by_hour IS NOT NULL AND v_rule.by_hour != '' THEN
        v_hours := ARRAY(
            SELECT CAST(trim(unnest) AS INTEGER)
            FROM unnest(string_to_array(v_rule.by_hour, ','))
            WHERE trim(unnest) ~ '^[0-9]+$'
        );
    END IF;
    
    IF v_rule.by_minute IS NOT NULL AND v_rule.by_minute != '' THEN
        v_minutes := ARRAY(
            SELECT CAST(trim(unnest) AS INTEGER)
            FROM unnest(string_to_array(v_rule.by_minute, ','))
            WHERE trim(unnest) ~ '^[0-9]+$'
        );
    END IF;
    
    IF v_rule.by_second IS NOT NULL AND v_rule.by_second != '' THEN
        v_seconds := ARRAY(
            SELECT CAST(trim(unnest) AS INTEGER)
            FROM unnest(string_to_array(v_rule.by_second, ','))
            WHERE trim(unnest) ~ '^[0-9]+$'
        );
    END IF;
    
    -- Convert week_start to integer (0=Sunday, 1=Monday, etc.)
    v_week_start := CASE v_rule.week_start
        WHEN 'SU' THEN 0
        WHEN 'MO' THEN 1
        WHEN 'TU' THEN 2
        WHEN 'WE' THEN 3
        WHEN 'TH' THEN 4
        WHEN 'FR' THEN 5
        WHEN 'SA' THEN 6
        ELSE 1  -- Default to Monday if unknown
    END;
    
    -- Start from event's start time, but adjust to start_date if needed
    v_current_date := v_event.start_time;
    
    -- If the requested start_date is after the event start, we need to calculate
    -- where to begin to avoid counting occurrences outside the range
    IF p_start_date > v_event.start_time THEN
        CASE v_rule.frequency
            WHEN 'DAILY' THEN
                -- Calculate how many intervals have passed since event start
                v_occurrence_count := EXTRACT(EPOCH FROM (p_start_date - v_event.start_time))::INTEGER / (86400 * v_rule.interval);
                v_current_date := v_event.start_time + (v_occurrence_count * v_rule.interval || ' days')::INTERVAL;
                
                -- If we're still before start_date, move to the next occurrence
                IF v_current_date < p_start_date THEN
                    v_current_date := v_current_date + (v_rule.interval || ' days')::INTERVAL;
                END IF;
                
            WHEN 'WEEKLY' THEN
                IF v_weekdays IS NOT NULL THEN
                    -- For weekly with by_weekday, we need to find the first valid occurrence at or after start_date
                    -- Start from the search start date but preserve the original event's time component
                    v_current_date := date_trunc('day', p_start_date) + 
                                      (EXTRACT(HOUR FROM v_event.start_time) || ' hours')::INTERVAL +
                                      (EXTRACT(MINUTE FROM v_event.start_time) || ' minutes')::INTERVAL +
                                      (EXTRACT(SECOND FROM v_event.start_time) || ' seconds')::INTERVAL;
                    
                    -- Find the first occurrence at or after start_date that matches a weekday
                    WHILE v_current_date <= p_end_date LOOP
                        IF EXTRACT(DOW FROM v_current_date) = ANY(v_weekdays) THEN
                            -- Check if this is a valid occurrence according to the recurrence pattern
                            -- Calculate how many days since the event start
                            v_days_ahead := EXTRACT(EPOCH FROM (v_current_date - v_event.start_time))::INTEGER / 86400;
                            
                            -- Check if this falls on a valid interval boundary
                            -- For weekly with specific weekdays, we need to check if this date
                            -- would naturally occur in the recurrence pattern
                            IF (v_days_ahead >= 0) THEN
                                EXIT; -- Found a valid starting point
                            END IF;
                        END IF;
                        v_current_date := v_current_date + INTERVAL '1 day';
                        
                        -- Safety check
                        IF v_current_date > p_start_date + INTERVAL '1 year' THEN
                            EXIT;
                        END IF;
                    END LOOP;
                ELSE
                    -- Calculate weeks passed for simple weekly
                    v_occurrence_count := EXTRACT(EPOCH FROM (p_start_date - v_event.start_time))::INTEGER / (604800 * v_rule.interval);
                    v_current_date := v_event.start_time + (v_occurrence_count * v_rule.interval * 7 || ' days')::INTERVAL;
                    
                    -- If we're still before start_date, move to the next occurrence
                    IF v_current_date < p_start_date THEN
                        v_current_date := v_current_date + (v_rule.interval * 7 || ' days')::INTERVAL;
                    END IF;
                END IF;
                
            WHEN 'MONTHLY' THEN
                -- For monthly, we need to be more careful, so start from original start_time
                -- and iterate until we reach the range
                WHILE v_current_date < p_start_date LOOP
                    v_current_date := v_current_date + (v_rule.interval || ' months')::INTERVAL;
                END LOOP;
                
            WHEN 'YEARLY' THEN
                -- For yearly, calculate years passed
                v_occurrence_count := EXTRACT(YEAR FROM p_start_date) - EXTRACT(YEAR FROM v_event.start_time);
                v_occurrence_count := v_occurrence_count / v_rule.interval;
                v_current_date := v_event.start_time + (v_occurrence_count * v_rule.interval || ' years')::INTERVAL;
                
                -- If we're still before start_date, move to the next occurrence
                IF v_current_date < p_start_date THEN
                    v_current_date := v_current_date + (v_rule.interval || ' years')::INTERVAL;
                END IF;
        END CASE;
    END IF;
    
        -- Reset occurrence count to count only occurrences within the range
    v_occurrence_count := 0;
    v_total_occurrences := 0; -- Track total actual occurrences from start
    
    -- If we moved the start date forward, calculate how many occurrences we've "skipped"
    -- to properly track count limits for the total series
    IF p_start_date > v_event.start_time AND v_rule.count IS NOT NULL THEN
        CASE v_rule.frequency
            WHEN 'DAILY' THEN
                -- Count how many daily occurrences would have happened by the search start time
                -- Only count non-cancelled occurrences (cancelled ones don't count toward limit)
                v_total_occurrences := 0;
                v_temp_date := v_event.start_time;
                
                WHILE v_temp_date < p_start_date LOOP
                    -- Check if this occurrence was cancelled
                    SELECT EXISTS(
                        SELECT 1 FROM calendar_integration_eventrecurrenceexception
                        WHERE parent_event_fk_id = p_event_id 
                        AND exception_date = v_temp_date
                        AND is_cancelled = true
                    ) INTO v_exception_exists;
                    
                    -- Only count non-cancelled occurrences toward limit
                    -- (cancelled exceptions before search range don't count)
                    IF NOT v_exception_exists THEN
                        v_total_occurrences := v_total_occurrences + 1;
                    END IF;
                    
                    -- Move to next day with interval
                    v_temp_date := v_temp_date + (v_rule.interval || ' days')::INTERVAL;
                    
                    -- Safety check
                    IF v_temp_date > p_start_date + INTERVAL '50 years' THEN
                        EXIT;
                    END IF;
                END LOOP;
            WHEN 'WEEKLY' THEN
                IF v_weekdays IS NOT NULL THEN
                    -- For weekly with by_weekday, count occurrences that would happen before search range
                    v_total_occurrences := 0;
                    v_temp_date := v_event.start_time;
                    
                    WHILE v_temp_date < p_start_date LOOP
                        -- Check if this date matches any specified weekday
                        IF EXTRACT(DOW FROM v_temp_date) = ANY(v_weekdays) THEN
                            -- Check if this occurrence was cancelled
                            SELECT EXISTS(
                                SELECT 1 FROM calendar_integration_eventrecurrenceexception
                                WHERE parent_event_fk_id = p_event_id 
                                AND exception_date = v_temp_date
                                AND is_cancelled = true
                            ) INTO v_exception_exists;
                            
                            -- Only count non-cancelled occurrences
                            IF NOT v_exception_exists THEN
                                v_total_occurrences := v_total_occurrences + 1;
                            END IF;
                        END IF;
                        
                        -- Simple daily increment to check all dates
                        v_temp_date := v_temp_date + INTERVAL '1 day';
                        
                        -- Safety check
                        IF v_temp_date > p_start_date + INTERVAL '50 years' THEN
                            EXIT;
                        END IF;
                    END LOOP;
                ELSE
                    -- For simple weekly without by_weekday, use faster calculation but then adjust for exceptions
                    v_total_occurrences := FLOOR(EXTRACT(EPOCH FROM (p_start_date - v_event.start_time)) / (604800 * v_rule.interval)) + 1;
                    
                    -- Subtract cancelled exceptions that occurred before the search range
                    SELECT COUNT(*) INTO v_temp_occurrence_count
                    FROM calendar_integration_eventrecurrenceexception
                    WHERE parent_event_fk_id = p_event_id 
                    AND exception_date < p_start_date
                    AND is_cancelled = true;
                    
                    v_total_occurrences := v_total_occurrences - v_temp_occurrence_count;
                END IF;
            WHEN 'MONTHLY' THEN
                -- For monthly, use faster calculation but then adjust for exceptions
                v_total_occurrences := FLOOR(((EXTRACT(YEAR FROM p_start_date) - EXTRACT(YEAR FROM v_event.start_time)) * 12 + 
                                     (EXTRACT(MONTH FROM p_start_date) - EXTRACT(MONTH FROM v_event.start_time))) / v_rule.interval) + 1;
                
                -- Subtract cancelled exceptions that occurred before the search range
                SELECT COUNT(*) INTO v_temp_occurrence_count
                FROM calendar_integration_eventrecurrenceexception
                WHERE parent_event_fk_id = p_event_id 
                AND exception_date < p_start_date
                AND is_cancelled = true;
                
                v_total_occurrences := v_total_occurrences - v_temp_occurrence_count;
                
            WHEN 'YEARLY' THEN
                -- For yearly, use faster calculation but then adjust for exceptions
                v_total_occurrences := FLOOR((EXTRACT(YEAR FROM p_start_date) - EXTRACT(YEAR FROM v_event.start_time)) / v_rule.interval) + 1;
                
                -- Subtract cancelled exceptions that occurred before the search range
                SELECT COUNT(*) INTO v_temp_occurrence_count
                FROM calendar_integration_eventrecurrenceexception
                WHERE parent_event_fk_id = p_event_id 
                AND exception_date < p_start_date
                AND is_cancelled = true;
                
                v_total_occurrences := v_total_occurrences - v_temp_occurrence_count;
            ELSE
                v_total_occurrences := 0;
        END CASE;
        
        -- Exit early if we've already exceeded the count limit before starting the search range
        IF v_total_occurrences >= v_rule.count THEN
            RETURN;
        END IF;
    END IF;
    
    -- Main loop to generate occurrences
    WHILE v_current_date <= p_end_date AND v_occurrence_count < v_max_occurrences LOOP
        -- Check termination conditions
        IF v_rule.until IS NOT NULL AND v_current_date > v_rule.until THEN
            EXIT;
        END IF;
        
        -- Apply BY* filters
        IF (v_months IS NULL OR EXTRACT(MONTH FROM v_current_date) = ANY(v_months)) AND
            (v_month_days IS NULL OR 
                EXTRACT(DAY FROM v_current_date) = ANY(v_month_days) OR
                EXISTS(
                    SELECT 1 FROM unnest(v_month_days) AS md 
                    WHERE md < 0 AND 
                    EXTRACT(DAY FROM v_current_date) = 
                    EXTRACT(DAY FROM (date_trunc('month', v_current_date) + interval '1 month - 1 day')) + md + 1
                )
            ) AND
            (v_weekdays IS NULL OR EXTRACT(DOW FROM v_current_date) = ANY(v_weekdays)) AND
            (v_year_days IS NULL OR 
                EXTRACT(DOY FROM v_current_date) = ANY(v_year_days) OR
                EXISTS(
                    SELECT 1 FROM unnest(v_year_days) AS yd 
                    WHERE yd < 0 AND 
                    EXTRACT(DOY FROM v_current_date) = 
                    (CASE WHEN EXTRACT(YEAR FROM v_current_date) % 4 = 0 AND 
                                (EXTRACT(YEAR FROM v_current_date) % 100 != 0 OR EXTRACT(YEAR FROM v_current_date) % 400 = 0)
                            THEN 366 ELSE 365 END) + yd + 1
                )
            ) AND
            (v_week_numbers IS NULL OR EXTRACT(WEEK FROM v_current_date) = ANY(v_week_numbers)) AND
            (v_hours IS NULL OR EXTRACT(HOUR FROM v_current_date) = ANY(v_hours)) AND
            (v_minutes IS NULL OR EXTRACT(MINUTE FROM v_current_date) = ANY(v_minutes)) AND
            (v_seconds IS NULL OR EXTRACT(SECOND FROM v_current_date) = ANY(v_seconds))
        THEN
            -- Check if current occurrence is within the requested range
            IF v_current_date >= p_start_date THEN
                -- Check for any exceptions (cancelled or modified)
                SELECT EXISTS(
                    SELECT 1 FROM calendar_integration_eventrecurrenceexception
                    WHERE parent_event_fk_id = p_event_id 
                    AND exception_date = v_current_date
                ) INTO v_exception_exists;
                
                IF NOT v_exception_exists THEN
                    -- No exception, so this is a regular occurrence
                    -- Increment total occurrences counter (for count limit tracking)
                    -- Only count non-cancelled occurrences toward the limit
                    v_total_occurrences := v_total_occurrences + 1;
                    
                    -- Check count limit after incrementing
                    IF v_rule.count IS NOT NULL AND v_total_occurrences > v_rule.count THEN
                        EXIT;
                    END IF;
                    
                    -- Regular occurrence - increment counter only for occurrences in range
                    v_occurrence_count := v_occurrence_count + 1;
                    
                    -- Return the occurrence
                    occurrence_start := v_current_date;
                    occurrence_end := v_current_date + v_duration;
                    is_exception := FALSE;
                    exception_type := NULL;
                    modified_event_id := NULL;
                    RETURN NEXT;
                    
                    -- Check if we've reached max occurrences
                    IF v_occurrence_count >= v_max_occurrences THEN
                        EXIT;
                    END IF;
                ELSE
                    -- Exception exists (cancelled or modified)
                    -- Check if it's cancelled
                    SELECT is_cancelled INTO v_exception_exists
                    FROM calendar_integration_eventrecurrenceexception
                    WHERE parent_event_fk_id = p_event_id 
                    AND exception_date = v_current_date;
                    
                    IF NOT v_exception_exists THEN
                        -- Modified (not cancelled) - count toward limit
                        v_total_occurrences := v_total_occurrences + 1;
                        
                        -- Check count limit after incrementing
                        IF v_rule.count IS NOT NULL AND v_total_occurrences > v_rule.count THEN
                            EXIT;
                        END IF;
                    END IF;
                    -- Note: Cancelled occurrences within search range don't count toward limit
                    -- and don't generate additional replacements (no gaps)
                    
                    -- Don't return the regular occurrence since there's an exception
                    -- (the modified occurrence will be added later if not cancelled)
                END IF;
            ELSE
                -- Occurrence is outside the search range, but still counts toward total limit
                -- Check if this occurrence is cancelled
                SELECT EXISTS(
                    SELECT 1 FROM calendar_integration_eventrecurrenceexception
                    WHERE parent_event_fk_id = p_event_id 
                    AND exception_date = v_current_date
                    AND is_cancelled = true
                ) INTO v_exception_exists;
                
                -- Only count non-cancelled occurrences toward limit
                IF NOT v_exception_exists THEN
                    v_total_occurrences := v_total_occurrences + 1;
                    
                    -- Check count limit after incrementing  
                    IF v_rule.count IS NOT NULL AND v_total_occurrences > v_rule.count THEN
                        EXIT;
                    END IF;
                END IF;
            END IF;
        END IF;
        
        -- Calculate next occurrence based on frequency
        CASE v_rule.frequency
            WHEN 'DAILY' THEN
                v_current_date := v_current_date + (v_rule.interval || ' days')::INTERVAL;
                
            WHEN 'WEEKLY' THEN
                IF v_weekdays IS NOT NULL THEN
                    -- Handle specific weekdays
                    v_found_in_week := FALSE;
                    
                    -- Look for next weekday in current week
                    FOR v_target_weekday IN SELECT unnest(v_weekdays) ORDER BY 1 LOOP
                        IF v_target_weekday > EXTRACT(DOW FROM v_current_date) THEN
                            v_days_ahead := v_target_weekday - EXTRACT(DOW FROM v_current_date);
                            v_current_date := v_current_date + (v_days_ahead || ' days')::INTERVAL;
                            v_found_in_week := TRUE;
                            EXIT;
                        END IF;
                    END LOOP;
                    
                    -- If no weekday found in current week, go to next week
                    IF NOT v_found_in_week THEN
                        -- Move to start of next week (considering week_start)
                        v_days_ahead := (7 - EXTRACT(DOW FROM v_current_date) + v_week_start) % 7;
                        IF v_days_ahead = 0 THEN v_days_ahead := 7; END IF;
                        v_current_date := v_current_date + (v_days_ahead || ' days')::INTERVAL;
                        
                        -- Find first occurrence in new week
                        v_target_weekday := (SELECT min(unnest) FROM unnest(v_weekdays));
                        v_days_ahead := (v_target_weekday - EXTRACT(DOW FROM v_current_date) + 7) % 7;
                        v_current_date := v_current_date + (v_days_ahead || ' days')::INTERVAL;
                        
                        -- Skip additional weeks based on interval
                        IF v_rule.interval > 1 THEN
                            v_current_date := v_current_date + ((v_rule.interval - 1) * 7 || ' days')::INTERVAL;
                        END IF;
                    END IF;
                ELSE
                    -- Simple weekly recurrence
                    v_current_date := v_current_date + (v_rule.interval * 7 || ' days')::INTERVAL;
                END IF;
                
            WHEN 'MONTHLY' THEN
                -- Add interval months
                v_current_date := v_current_date + (v_rule.interval || ' months')::INTERVAL;
                
                -- Handle month day constraints
                IF v_month_days IS NOT NULL THEN
                    -- Find next valid month day
                    v_temp_date := date_trunc('month', v_current_date);
                    v_found_in_week := FALSE;
                    
                    FOR v_target_weekday IN SELECT unnest(v_month_days) ORDER BY 1 LOOP
                        IF v_target_weekday > 0 THEN
                            -- Positive day number
                            IF v_target_weekday <= EXTRACT(DAY FROM (v_temp_date + interval '1 month - 1 day')) THEN
                                v_current_date := v_temp_date + (v_target_weekday - 1 || ' days')::INTERVAL;
                                v_current_date := v_current_date + 
                                    (EXTRACT(HOUR FROM v_event.start_time) || ' hours')::INTERVAL +
                                    (EXTRACT(MINUTE FROM v_event.start_time) || ' minutes')::INTERVAL +
                                    (EXTRACT(SECOND FROM v_event.start_time) || ' seconds')::INTERVAL;
                                v_found_in_week := TRUE;
                                EXIT;
                            END IF;
                        ELSE
                            -- Negative day number (-1 = last day of month)
                            v_current_date := (v_temp_date + interval '1 month - 1 day') + (v_target_weekday + 1 || ' days')::INTERVAL;
                            v_current_date := v_current_date + 
                                (EXTRACT(HOUR FROM v_event.start_time) || ' hours')::INTERVAL +
                                (EXTRACT(MINUTE FROM v_event.start_time) || ' minutes')::INTERVAL +
                                (EXTRACT(SECOND FROM v_event.start_time) || ' seconds')::INTERVAL;
                            v_found_in_week := TRUE;
                            EXIT;
                        END IF;
                    END LOOP;
                    
                    IF NOT v_found_in_week THEN
                        -- No valid day found, skip this month
                        CONTINUE;
                    END IF;
                END IF;
                
            WHEN 'YEARLY' THEN
                v_current_date := v_current_date + (v_rule.interval || ' years')::INTERVAL;
                
            ELSE
                EXIT; -- Unknown frequency
        END CASE;
    END LOOP;
    
    -- Add modified exceptions within the date range
    FOR occurrence_start, occurrence_end, is_exception, exception_type, modified_event_id IN
        SELECT 
            me.start_time,
            me.end_time,
            TRUE,
            'modified',
            me.id
        FROM calendar_integration_eventrecurrenceexception re
        JOIN calendar_integration_calendarevent me ON re.modified_event_fk_id = me.id
        WHERE re.parent_event_fk_id = p_event_id
        AND NOT re.is_cancelled
        AND me.start_time >= p_start_date 
        AND me.start_time <= p_end_date
    LOOP
        RETURN NEXT;
    END LOOP;
    
    RETURN;
END;
$$ LANGUAGE plpgsql;
