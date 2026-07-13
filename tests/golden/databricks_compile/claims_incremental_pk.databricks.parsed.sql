-- adapter: databricks
-- artifact: parsed_model_body (dbt parse raw_code; not compiled)
-- materialized: incremental
-- contract_enforced: True
{{
    config(
        materialized='incremental',
        incremental_strategy='merge',
        file_format='delta',
        unique_key=["claim_id"],
        on_schema_change='append_new_columns',
        contract={'enforced': True},
    )
}}

-- incremental + primary_key: dbt MERGEs on unique_key.
-- The dedup-latest window keeps the newest row per key in the batch.
SELECT
        claim_id                                                                          AS claim_id,
        cast(nullif(trim(regexp_replace(claim_amount, '^\\$', '')), '') as DECIMAL(18,2)) AS claim_amount,
        cast(try_to_timestamp(service_date, 'yyyyMMdd') as date)                          AS service_date,
        try_to_timestamp(submitted_at, 'MM/dd/yyyy HH:mm:ss')                             AS submitted_at,
        member_id                                                                         AS member_id,
        cast(is_paid as boolean)                                                          AS is_paid
FROM (
        SELECT * FROM (
            SELECT *, row_number() OVER (PARTITION BY claim_id ORDER BY _load_ts DESC) AS _rn
            FROM {{ source('raw', 'raw_claims') }}
        ) WHERE _rn = 1
) AS src_raw
