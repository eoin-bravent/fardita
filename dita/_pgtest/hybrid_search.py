#!/usr/bin/env python3
"""Hybrid retrieval over the FAR: vector + keyword lanes fused with RRF,
then a shallow graph expansion of the top hit.
Run with Python 3.12:   py -3.12 hybrid_search.py "your question"
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from load_neon import load_env, connect
from fastembed import TextEmbedding

K = 60          # RRF constant
LANE_N = 30     # candidates per lane
MODEL = "BAAI/bge-small-en-v1.5"

query = sys.argv[1] if len(sys.argv) > 1 else "when can an agency skip publicizing for urgency"
conn = connect(load_env()); cur = conn.cursor()

# --- embed the query (same model as the chunks) ---
qvec = "[" + ",".join(f"{x:.6f}" for x in next(TextEmbedding(MODEL).embed([query]))) + "]"

# --- lane 1: semantic (cosine via HNSW) ---
cur.execute("""SELECT chunk_id, far_address, title FROM chunks
               WHERE embedding IS NOT NULL
               ORDER BY embedding <=> %s::vector LIMIT %s""", (qvec, LANE_N))
vec = cur.fetchall()

# --- lane 2: lexical (tsvector) ---
cur.execute("""SELECT chunk_id, far_address, title FROM chunks,
               websearch_to_tsquery('english', %s) q
               WHERE tsv @@ q ORDER BY ts_rank(tsv, q) DESC LIMIT %s""", (query, LANE_N))
lex = cur.fetchall()

# --- Reciprocal Rank Fusion ---
score, meta = {}, {}
for lane_name, rows in (("vec", vec), ("lex", lex)):
    for rank, (cid, addr, title) in enumerate(rows, 1):
        score[cid] = score.get(cid, 0) + 1.0 / (K + rank)
        meta.setdefault(cid, (addr, title, set()))[2].add(lane_name)

fused = sorted(score.items(), key=lambda kv: kv[1], reverse=True)

print(f'query: "{query}"\n')
print(f"{'rank':<5}{'far_addr':<14}{'lanes':<10}{'rrf':<8}title")
print("-" * 80)
for i, (cid, s) in enumerate(fused[:10], 1):
    addr, title, lanes = meta[cid]
    print(f"{i:<5}{(addr or ''):<14}{'+'.join(sorted(lanes)):<10}{s:<8.4f}{(title or '')[:42]}")

# --- graph expansion of the top hit ---
top_cid = fused[0][0]
cur.execute("SELECT content_item_id FROM chunks WHERE chunk_id=%s", (top_cid,))
item = cur.fetchone()[0]
print(f"\ngraph 1-hop from top hit ({item}):")
cur.execute("""SELECT confidence, anchor_text, to_item FROM relationships
               WHERE from_item=%s ORDER BY confidence""", (item,))
for conf, anchor, to in cur.fetchall() or []:
    print(f"   {conf:<8} {anchor or '':<28} -> {to or ''}")
cur.close(); conn.close()
