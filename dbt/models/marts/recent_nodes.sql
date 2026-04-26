-- [[file:../../../../../org/20260425230731-org_llm.org::*recent_nodes.sql][recent_nodes.sql:1]]
select *
from {{ ref('stg_nodes') }}
where days_since_modified <= 30
  -- exclude this via stg_nodes join to stg_files
order by mtime desc
-- recent_nodes.sql:1 ends here
