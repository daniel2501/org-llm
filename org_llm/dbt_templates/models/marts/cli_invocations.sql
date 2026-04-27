-- cli_invocations — every `org-llm <verb>` run, with outcome + duration.
-- Powers "what verbs do I actually use", "how often does setup crash",
-- "which command takes longest". The outcome column normalises
-- exit:N codes to a flat "ok / error / interrupted / crash / refused".
with normalised as (
    select
        id,
        event_at,
        timestamp,
        command            as verb,
        args               as argv,
        outcome,
        duration_ms,
        response,
        case
            when outcome = 'ok' then 'ok'
            when outcome = 'interrupted' then 'interrupted'
            when outcome = 'crash' then 'crash'
            when outcome = 'refused' then 'refused'
            when outcome like 'exit:%' then 'error'
            when outcome like 'unrecovered%' then 'error'
            else outcome
        end                as outcome_class
    from {{ ref('stg_history') }}
    where kind = 'cli'
)
select * from normalised
order by event_at desc
