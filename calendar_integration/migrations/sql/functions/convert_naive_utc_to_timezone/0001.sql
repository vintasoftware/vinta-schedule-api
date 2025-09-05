CREATE OR REPLACE FUNCTION convert_naive_utc_to_timezone(
    utc_timestamp TIMESTAMPTZ,
    target_timezone TEXT
) RETURNS TIMESTAMPTZ AS $$
BEGIN
    IF target_timezone IS NULL OR target_timezone = '' THEN
        RETURN utc_timestamp;
    END IF;
    
    -- Treat the UTC timestamp as a naive datetime and attach the target timezone
    -- This effectively ignores the UTC timezone and treats it as local time in target_timezone
    RETURN (utc_timestamp AT TIME ZONE 'UTC')::TIMESTAMP AT TIME ZONE target_timezone;
END;
$$ LANGUAGE plpgsql IMMUTABLE;
