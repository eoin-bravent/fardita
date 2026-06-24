package com.far.farapi;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.List;

/**
 * Retrieval over the FAR tables in Neon. All Postgres-specific SQL (tsvector,
 * pgvector, recursive CTEs) is isolated here, behind plain method calls -- the
 * data-access boundary the design docs call for.
 */
@Repository
public class FarRetriever {

    private final JdbcTemplate jdbc;

    public FarRetriever(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public record Hit(String contentItemId, String farAddress, String title, String text) {}
    public record Ref(String confidence, String anchorText, String toItem) {}

    /** Keyword lane (tsvector full-text). Works today, no embedding model needed. */
    public List<Hit> keywordSearch(String query, int k) {
        return jdbc.query("""
                SELECT content_item_id, far_address, title, canonical_text
                FROM chunks, websearch_to_tsquery('english', ?) tsq
                WHERE tsv @@ tsq
                ORDER BY ts_rank(tsv, tsq) DESC
                LIMIT ?
                """,
            (rs, i) -> new Hit(rs.getString(1), rs.getString(2), rs.getString(3), rs.getString(4)),
            query, k);
    }

    /** Graph 1-hop: outgoing references of a content item, high/medium confidence first. */
    public List<Ref> references(String contentItemId) {
        return jdbc.query("""
                SELECT confidence, anchor_text, to_item
                FROM relationships
                WHERE from_item = ?
                ORDER BY CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END
                """,
            (rs, i) -> new Ref(rs.getString(1), rs.getString(2), rs.getString(3)),
            contentItemId);
    }

    // ----------------------------------------------------------------------
    // VECTOR LANE (add once query embeddings use BAAI/bge-small-en-v1.5):
    //
    //   public List<Hit> vectorSearch(float[] qvec, int k) {
    //       String v = java.util.Arrays.toString(qvec);   // "[0.1,0.2,...]"
    //       return jdbc.query("""
    //           SELECT content_item_id, far_address, title, canonical_text
    //           FROM chunks WHERE embedding IS NOT NULL
    //           ORDER BY embedding <=> ?::vector LIMIT ?""",
    //           (rs,i) -> new Hit(rs.getString(1),rs.getString(2),rs.getString(3),rs.getString(4)),
    //           v, k);
    //   }
    //
    // Then fuse keyword + vector with RRF (port hybrid_search.py).
    // ----------------------------------------------------------------------
}
