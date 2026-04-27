-- llm_calls — every LLM round-trip (chat + embed) with model + outcome
-- + latency. Lets the user see "how many times did I call gemma3 this
-- week", "what's the median latency for nomic-embed-text vs Ollama vs
-- cloud", "which model fails most often". Rolls up to per-model stats
-- via the recent_llm_activity mart below.
select
    id,
    event_at,
    timestamp,
    command,
    model,
    duration_ms,
    outcome,
    args,
    response,
    -- Derived: was this a chat or an embed call?
    case
        when command = 'embed' then 'embed'
        when command = 'chat'  then 'chat'
        else command
    end                                 as call_kind
from {{ ref('stg_history') }}
where kind = 'llm'
order by event_at desc
