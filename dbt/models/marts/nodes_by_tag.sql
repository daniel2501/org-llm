-- [[file:../../../../../org/20260425230731-org_llm.org::*nodes_by_tag.sql][nodes_by_tag.sql:1]]
with tag_rows as (
    select
        trim(value)  as tag,
        id,
        title,
        file_path
    from {{ ref('stg_nodes') }},
         json_each('["' || replace(replace(tags, ' ', '","'), ':', '') || '"]')
    where tags != ''
)
select
    tag,
    count(*)                           as node_count,
    group_concat(title, ' | ')         as titles
from tag_rows
group by tag
order by node_count desc
-- nodes_by_tag.sql:1 ends here
