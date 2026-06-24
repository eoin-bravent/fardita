-- Things to try once the data is loaded.  Run:  psql -d far_test -f queries.sql
-- (or paste them one at a time to actually read the output)

\echo '== 1. LEXICAL: full-text search for an exact-term question =='
-- "combined synopsis/solicitation SF1449" -> should rank 12.603 chunks top
SELECT far_address, ts_rank(tsv, query) AS rank
FROM chunks, websearch_to_tsquery('english', 'combined synopsis solicitation SF1449') query
WHERE tsv @@ query
ORDER BY rank DESC
LIMIT 5;

\echo '== 2. LEXICAL: a semantic-ish phrasing the keyword lane handles poorly =='
-- "how long do vendors get to respond" -> note weaker/looser matches (this is
-- the gap Phase-2 vector search will fill)
SELECT far_address, ts_rank(tsv, query) AS rank
FROM chunks, websearch_to_tsquery('english', 'how long vendors respond bid') query
WHERE tsv @@ query
ORDER BY rank DESC
LIMIT 5;

\echo '== 3. FUZZY: recover a malformed citation with trigram similarity =='
SELECT far_address, similarity(far_address, '5.203') AS sim
FROM chunks
WHERE far_address % '5.203'
ORDER BY sim DESC
LIMIT 5;

\echo '== 4. GRAPH: 1-hop expansion from a seed paragraph, with confidence =='
-- seed = 5.203(g).  Show what it references (resolved targets + whether loaded)
SELECT r.confidence, r.anchor_text, r.to_item,
       ci.far_address AS target_addr,
       (ci.content_item_id IS NOT NULL) AS target_loaded,
       r.review_required
FROM relationships r
LEFT JOIN content_items ci ON ci.content_item_id = r.to_item
WHERE r.from_item = 'FAR_5_203_g'
ORDER BY r.confidence;

\echo '== 5. GRAPH: 2-hop multi-hop (the Q12 path) =='
-- 5.203(g) -> 5.202(a)(2) -> 6.302-2 ... follow resolved references 2 deep
WITH RECURSIVE walk AS (
    SELECT from_item, to_item, 1 AS hop
    FROM relationships
    WHERE from_item = 'FAR_5_203_g' AND to_item IS NOT NULL
  UNION ALL
    SELECT r.from_item, r.to_item, w.hop + 1
    FROM relationships r
    JOIN walk w ON r.from_item = w.to_item
    WHERE w.hop < 3 AND r.to_item IS NOT NULL
)
SELECT DISTINCT hop, to_item, ci.far_address, ci.title
FROM walk LEFT JOIN content_items ci ON ci.content_item_id = walk.to_item
ORDER BY hop;

\echo '== 6. GRAPH: parent context for a paragraph (containment / breadcrumb) =='
WITH RECURSIVE up AS (
    SELECT content_item_id, parent_id, far_address, item_type
    FROM content_items WHERE content_item_id = 'FAR_5_203_g'
  UNION ALL
    SELECT c.content_item_id, c.parent_id, c.far_address, c.item_type
    FROM content_items c JOIN up ON c.content_item_id = up.parent_id
)
SELECT item_type, far_address FROM up;

\echo '== 7. HYBRID preview: lexical hit + its graph neighbours in one go =='
-- find the best lexical chunk for a query, then attach its outgoing references
WITH hit AS (
    SELECT content_item_id, far_address
    FROM chunks, websearch_to_tsquery('english', 'urgency exception synopsis') q
    WHERE tsv @@ q ORDER BY ts_rank(tsv, q) DESC LIMIT 1
)
SELECT hit.far_address AS seed, r.confidence, r.anchor_text, r.to_item
FROM hit JOIN relationships r ON r.from_item = hit.content_item_id
ORDER BY r.confidence;

\echo '== counts =='
SELECT 'content_items' t, count(*) FROM content_items
UNION ALL SELECT 'chunks', count(*) FROM chunks
UNION ALL SELECT 'relationships', count(*) FROM relationships
UNION ALL SELECT '  medium-conf (ambiguous)', count(*) FROM relationships WHERE confidence='medium'
UNION ALL SELECT '  low-conf (ranges, review)', count(*) FROM relationships WHERE confidence='low';
