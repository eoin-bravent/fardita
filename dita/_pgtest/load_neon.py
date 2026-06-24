#!/usr/bin/env python3
"""Load the FAR Subpart 5.2 fixture into Neon and run a few demo queries.

Uses pg8000 (pure-Python, installs fine on 3.14). Reads DATABASE_URL from the
environment or from the local .env file. No psql, no local Postgres needed.
"""
import os, re, ssl, sys
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import parse_to_sql as P            # importing runs the parser -> P.items / P.chunks / P.rels
import pg8000.dbapi

def load_env():
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    envp = os.path.join(HERE, ".env")
    if os.path.exists(envp):
        for line in open(envp, encoding="utf-8"):
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    sys.exit("No DATABASE_URL found (set env var or .env)")

def connect(url):
    u = urlparse(url)
    return pg8000.dbapi.connect(
        user=u.username, password=u.password,
        host=u.hostname, port=u.port or 5432,
        database=u.path.lstrip("/"),
        ssl_context=ssl.create_default_context(),
    )

def exec_script(cur, path):
    sql = open(path, encoding="utf-8").read()
    # strip -- comments first (some contain ';'), then split on statement ';'
    lines = [ln[:ln.index("--")] if "--" in ln else ln for ln in sql.splitlines()]
    clean = "\n".join(lines)
    for stmt in (s.strip() for s in clean.split(";")):
        if stmt:
            cur.execute(stmt)

def bulk_insert(cur, table, cols, rows, batch=1000):
    if not rows:
        return
    ph = "(" + ",".join(["%s"] * len(cols)) + ")"
    head = f"INSERT INTO {table} ({','.join(cols)}) VALUES "
    for i in range(0, len(rows), batch):     # batch to stay under the 65535-param limit
        chunk = rows[i:i + batch]
        sql = head + ",".join([ph] * len(chunk))
        cur.execute(sql, [v for row in chunk for v in row])

def dedupe(rows, keyidx=0):
    seen, out = set(), []
    for r in rows:
        if r[keyidx] not in seen:
            seen.add(r[keyidx]); out.append(r)
    return out

def show(cur, sql, label):
    cur.execute(sql)
    rows = cur.fetchall()
    print(f"\n--- {label} ---")
    for r in rows:
        print("   " + " | ".join("" if v is None else str(v) for v in r))

def main():
    url = load_env()
    conn = connect(url)
    cur = conn.cursor()

    print("connected:", end=" ")
    cur.execute("select version()")
    print(cur.fetchone()[0].split(",")[0])

    # Phase-2 readiness check: is pgvector available on this Neon project?
    cur.execute("select 1 from pg_available_extensions where name='vector'")
    print("pgvector available:", bool(cur.fetchone()))

    files = P.all_files() if os.environ.get("FAR_ALL") else P.FILES
    print(f"\nparsing {len(files)} files...")
    fails, skips = P.run(files)
    print(f"  parse failures={len(fails)}  skipped(no concept)={len(skips)}")

    items = dedupe(P.items)
    chnks = dedupe(P.chunks)
    print(f"  content_items={len(items)}  chunks={len(chnks)}  relationships={len(P.rels)}")

    print("building schema...")
    exec_script(cur, os.path.join(HERE, "schema.sql"))

    print("loading data...")
    bulk_insert(cur, "content_items",
                ["content_item_id","item_type","far_address","parent_id",
                 "title","breadcrumb","depth","retrievable"], items)
    bulk_insert(cur, "chunks",
                ["chunk_id","content_item_id","far_address","title",
                 "breadcrumb","canonical_text","enriched_text"], chnks)
    bulk_insert(cur, "relationships",
                ["from_item","to_item","rel_type","confidence",
                 "anchor_text","target_raw","review_required"], P.rels)
    conn.commit()

    # ---- demo queries -------------------------------------------------
    show(cur, """
        SELECT 'content_items', count(*) FROM content_items
        UNION ALL SELECT 'chunks', count(*) FROM chunks
        UNION ALL SELECT 'relationships', count(*) FROM relationships
        UNION ALL SELECT '  medium (ambiguous)', count(*) FROM relationships WHERE confidence='medium'
        UNION ALL SELECT '  low (ranges, review)', count(*) FROM relationships WHERE confidence='low'
    """, "counts")

    show(cur, """
        SELECT far_address, round(ts_rank(tsv, q)::numeric, 4) AS rank
        FROM chunks, websearch_to_tsquery('english','combined synopsis solicitation SF1449') q
        WHERE tsv @@ q ORDER BY rank DESC LIMIT 5
    """, "LEXICAL: 'combined synopsis solicitation SF1449'")

    show(cur, """
        SELECT r.confidence, r.anchor_text, r.to_item,
               (ci.content_item_id IS NOT NULL) AS target_loaded, r.review_required
        FROM relationships r
        LEFT JOIN content_items ci ON ci.content_item_id = r.to_item
        WHERE r.from_item = 'FAR_5_203_g'
        ORDER BY r.confidence
    """, "GRAPH 1-hop from 5.203(g)")

    show(cur, """
        WITH RECURSIVE walk AS (
            SELECT from_item, to_item, 1 AS hop FROM relationships
            WHERE from_item='FAR_5_203_g' AND to_item IS NOT NULL
          UNION ALL
            SELECT r.from_item, r.to_item, w.hop+1 FROM relationships r
            JOIN walk w ON r.from_item=w.to_item
            WHERE w.hop<3 AND r.to_item IS NOT NULL
        )
        SELECT DISTINCT hop, walk.to_item, ci.far_address, ci.title
        FROM walk LEFT JOIN content_items ci ON ci.content_item_id=walk.to_item
        ORDER BY hop
    """, "GRAPH multi-hop: 5.203(g) -> 5.202(a)(2) -> ...")

    cur.close(); conn.close()
    print("\ndone.")

if __name__ == "__main__":
    main()
