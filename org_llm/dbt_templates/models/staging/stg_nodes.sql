-- stg_nodes — clean nodes view layered over the raw indexer table.
--
-- Exposes both tag buckets so downstream marts can choose: the merged
-- set (most natural for tag analytics) or just one bucket (e.g. when
-- reasoning about LLM-vs-human provenance).
select
    n.id,
    n.node_id,
    n.title,
    n.body,
    n.tags,
    n.auto_tags,
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
