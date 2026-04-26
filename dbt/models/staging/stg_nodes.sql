-- [[file:../../../../../org/20260425230731-org_llm.org::*stg_nodes.sql][stg_nodes.sql:1]]
select
    n.id,
    n.node_id,
    n.title,
    n.body,
    n.tags,
    n.mtime,
    f.path                                    as file_path,
    trim(replace(f.path, (
        select value from config where key = 'org_dir'
    ), ''))                                   as relative_path,
    datetime(n.mtime, 'unixepoch', 'localtime') as modified_at,
    case
        when n.embedding is not null then 1
        else 0
    end                                       as has_embedding
from nodes n
join files f on f.id = n.file_id
-- stg_nodes.sql:1 ends here
