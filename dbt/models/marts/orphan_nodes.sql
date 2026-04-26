-- [[file:../../../../../org/20260425230731-org_llm.org::*orphan_nodes.sql][orphan_nodes.sql:1]]
select n.*
from {{ ref('stg_nodes') }} n
where n.node_id is not null
  and n.node_id not in (
      select value
      from {{ ref('stg_nodes') }},
           json_each('[]')   -- placeholder: links table added in Phase 2
      where 1=0
  )
-- orphan_nodes.sql:1 ends here
