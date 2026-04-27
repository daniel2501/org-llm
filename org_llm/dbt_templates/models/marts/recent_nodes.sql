-- recent_nodes — nodes modified in the last 30 days. We compute
-- days_since_modified inline rather than depending on stg_files (which
-- has file-level granularity); inlining keeps this mart a single-source
-- downstream view of stg_nodes.
select
    n.*,
    round(julianday('now') - julianday(n.modified_at), 1) as days_since_modified
from {{ ref('stg_nodes') }} n
where round(julianday('now') - julianday(n.modified_at), 1) <= 30
order by n.mtime desc
