-- [[file:../../../../../org/20260425230731-org_llm.org::*stg_files.sql][stg_files.sql:1]]
select
    id,
    path,
    node_count,
    datetime(indexed_at)                                  as indexed_at,
    datetime(mtime, 'unixepoch', 'localtime')             as modified_at,
    round((julianday('now') - julianday(datetime(mtime, 'unixepoch', 'localtime'))), 1)
                                                          as days_since_modified
from files
-- stg_files.sql:1 ends here
