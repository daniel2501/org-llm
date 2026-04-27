-- nodes_by_tag — tag → count + titles. Tags live in TWO buckets in
-- the indexer schema: nodes.tags (file source-of-truth) and nodes.auto_tags
-- (LLM-applied). The merged set is what users mean when they ask
-- "what notes are tagged X". Each row is one (tag, node) pair, then
-- aggregated.
with merged as (
    select
        n.id        as nb_node_id,
        n.title,
        n.file_path,
        trim(coalesce(n.tags, '') || ' ' || coalesce(n.auto_tags, '')) as merged_tags
    from {{ ref('stg_nodes') }} n
    where coalesce(n.tags, '') <> '' or coalesce(n.auto_tags, '') <> ''
),
exploded as (
    select
        m.nb_node_id,
        m.title,
        m.file_path,
        trim(j.value) as tag
    from merged m,
         json_each('["' || replace(replace(m.merged_tags, ' ', '","'), ':', '') || '"]') j
    where trim(j.value) <> ''
)
select
    tag,
    count(distinct nb_node_id)         as node_count,
    group_concat(distinct title)       as titles
from exploded
group by tag
order by node_count desc
