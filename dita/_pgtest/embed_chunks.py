#!/usr/bin/env python3
"""Populate chunks.embedding with a local CPU model (no GPU, no API key).
Run with Python 3.12:   py -3.12 embed_chunks.py
Requires: pip install fastembed pg8000
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from load_neon import load_env, connect
from fastembed import TextEmbedding

MODEL = "BAAI/bge-small-en-v1.5"   # 384-dim, ~130MB, CPU
WRITE_BATCH = 500

conn = connect(load_env()); cur = conn.cursor()
cur.execute("SELECT chunk_id, enriched_text FROM chunks WHERE embedding IS NULL ORDER BY chunk_id")
rows = cur.fetchall()
print(f"to embed: {len(rows)}")
if not rows:
    sys.exit("nothing to embed")

ids = [r[0] for r in rows]
texts = [r[1] for r in rows]

print(f"loading model {MODEL} (first run downloads ~130MB)...")
model = TextEmbedding(MODEL)

t0 = time.time()
buf = []
for n, (cid, vec) in enumerate(zip(ids, model.embed(texts, batch_size=64)), 1):
    vecstr = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
    buf.append((vecstr, cid))
    if len(buf) >= WRITE_BATCH:
        cur.executemany("UPDATE chunks SET embedding = %s::vector WHERE chunk_id = %s", buf)
        conn.commit(); buf = []
        print(f"  {n}/{len(rows)}  ({n/(time.time()-t0):.0f}/s)")
if buf:
    cur.executemany("UPDATE chunks SET embedding = %s::vector WHERE chunk_id = %s", buf)
    conn.commit()

cur.execute("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
print(f"done in {time.time()-t0:.0f}s; embedded rows now: {cur.fetchone()[0]}")
cur.close(); conn.close()
