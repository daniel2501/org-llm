-- stg_history — clean view over the raw `history` event log written
-- by org_llm/logbook.py. Coalesces NULLs from the additive-migration
-- columns (kind/model/args/duration_ms/outcome) so downstream marts
-- can assume non-null strings + integers.
select
    id,
    timestamp,
    coalesce(kind, '')          as kind,
    coalesce(command, '')       as command,
    coalesce(query, '')         as args_legacy,
    coalesce(args, '')          as args,
    coalesce(response, '')      as response,
    coalesce(model, '')         as model,
    coalesce(duration_ms, 0)    as duration_ms,
    coalesce(outcome, '')       as outcome,
    -- Convenience: a rough event_at as a real datetime for grouping.
    datetime(timestamp)         as event_at
from history
