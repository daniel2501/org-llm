-- daily_notes — files under any `/daily/` subdir, regardless of where
-- org_dir is mounted on disk.
--
-- We filter on `file_path` (the full absolute path) rather than
-- `relative_path` because the latter requires SQLite to know how to
-- expand `~/org` from the config row, which it doesn't — when
-- org_dir is stored as a literal tilde, the prefix-strip in
-- stg_nodes is a no-op and `relative_path == file_path`. Substring
-- match works in both cases.
select *
from {{ ref('stg_nodes') }}
where file_path like '%/daily/%'
order by mtime desc
