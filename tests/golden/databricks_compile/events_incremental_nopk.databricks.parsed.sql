-- adapter: databricks
-- artifact: parsed_model_body (dbt parse raw_code; not compiled)
-- materialized: incremental
-- contract_enforced: True
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
        event_type                                                                  AS event_type,
        payload                                                                     AS payload,
        try_to_timestamp(occurred_at, 'yyyy-MM-dd HH:mm:ss')                        AS occurred_at,
        cast(nullif(trim(regexp_replace(amount, '^\\$', '')), '') as DECIMAL(10,2)) AS amount
FROM {{ source('raw', 'raw_events') }}
