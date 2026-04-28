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
    -- relative_path: file_path with the org_dir stripped from the
    -- front. SQLite has no expanduser, so when the user's `org_dir`
    -- config row holds a literal `~/org` (the default) the naive
    -- replace() against an absolute file_path is a no-op. Workaround:
    -- compute the absolute org_dir at query time by stripping the
    -- shortest filename suffix from the FIRST file_path that contains
    -- the bare basename of org_dir. Falls back to file_path when the
    -- heuristic finds nothing — then `relative_path == file_path` and
    -- substring matches in downstream marts still work.
    case
        when f.path like '%/' || (
            select replace(value, '~/', '') from config where key='org_dir'
        ) || '/%'
            then substr(f.path, instr(f.path,
                '/' || (select replace(value, '~/', '')
                        from config where key='org_dir') || '/')
                + length((select replace(value, '~/', '')
                        from config where key='org_dir')) + 1)
        else f.path
    end                                       as relative_path,
    datetime(n.mtime, 'unixepoch', 'localtime') as modified_at,
    case
        when n.embedding is not null then 1
        else 0
    end                                       as has_embedding
from nodes n
join files f on f.id = n.file_id
