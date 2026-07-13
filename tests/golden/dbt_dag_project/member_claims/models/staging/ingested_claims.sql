{{
    config(
        materialized='incremental',
        incremental_strategy='merge',
        unique_key=["claim_id"],
        on_schema_change='append_new_columns',
        contract={'enforced': True},
    )
}}

-- incremental + primary_key: dbt MERGEs on unique_key.
-- The dedup-latest window keeps the newest row per key in the batch.
SELECT
        try_cast(nullif(trim(regexp_replace(claim_id, '^\$', '')), '') as INT)               AS claim_id,
        try_cast(nullif(trim(regexp_replace(member_id, '^\$', '')), '') as INT)              AS member_id,
        try_cast(nullif(trim(regexp_replace(claim_amount, '^\$', '')), '') as DECIMAL(18,2)) AS claim_amount
FROM (
        SELECT * FROM (
            SELECT *, row_number() OVER (PARTITION BY claim_id ORDER BY _load_ts DESC) AS _rn
            FROM {{ source('raw', 'raw_claims') }}
        ) WHERE _rn = 1
) AS src_raw
