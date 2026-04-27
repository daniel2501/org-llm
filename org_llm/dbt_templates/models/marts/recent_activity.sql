-- recent_activity — last 7 days of EVERY logged event (cli + llm + mcp
-- + config + doctor + dbt). Use as the entry point for "what was I
-- doing yesterday" or to feed a heartbeat dashboard.
select
    id,
    event_at,
    timestamp,
    kind,
    command,
    outcome,
    duration_ms,
    model
from {{ ref('stg_history') }}
where julianday('now') - julianday(event_at) <= 7
order by event_at desc
