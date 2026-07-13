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
        claim_id                                                                                                                                   AS claim_id,
        try_cast(nullif(trim(regexp_replace(claim_amount, '^\$', '')), '') as DECIMAL(18,2))                                                       AS claim_amount,
        cast(case when regexp_full_match(service_date, '\d{4}\d{2}\d{2}') then try_strptime(service_date, '%Y%m%d') end as date)                   AS service_date,
        case when regexp_full_match(submitted_at, '\d{2}/\d{2}/\d{4}\ \d{2}:\d{2}:\d{2}') then try_strptime(submitted_at, '%m/%d/%Y %H:%M:%S') end AS submitted_at,
        member_id                                                                                                                                  AS member_id,
        try_cast(is_paid as boolean)                                                                                                               AS is_paid
FROM (
        SELECT * FROM (
            SELECT *, row_number() OVER (PARTITION BY claim_id ORDER BY _load_ts DESC) AS _rn
            FROM {{ source('raw', 'raw_claims') }}
        ) WHERE _rn = 1
) AS src_raw
