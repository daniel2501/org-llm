-- [[file:../../../../../org/20260425230731-org_llm.org::*daily_notes.sql][daily_notes.sql:1]]
select *
from {{ ref('stg_nodes') }}
where relative_path like '/daily/%'
order by mtime desc
-- daily_notes.sql:1 ends here
