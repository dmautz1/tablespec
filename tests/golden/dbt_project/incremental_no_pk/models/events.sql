{{
    config(
        materialized='incremental',
        on_schema_change='append_new_columns',
        contract={'enforced': True},
    )
}}

-- WARNING: no primary_key + incremental -> blind append (no dedup).
-- Contract: raw source holds ONE batch per run; duplicates accumulate
-- on re-ingest of the same rows (matches the Spark INSERT INTO branch).
SELECT
        event_type                                                                                                                                 AS event_type,
        payload                                                                                                                                    AS payload,
        case when regexp_full_match(occurred_at, '\d{4}\-\d{2}\-\d{2}\ \d{2}:\d{2}:\d{2}') then try_strptime(occurred_at, '%Y-%m-%d %H:%M:%S') end AS occurred_at,
        try_cast(nullif(trim(regexp_replace(amount, '^\$', '')), '') as DECIMAL(10,2))                                                             AS amount
FROM {{ source('raw', 'raw_events') }}
