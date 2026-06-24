#!/usr/bin/env python3
"""Tiny query runner against Neon.  Usage:  python q.py "SELECT ..."
Reuses the connection helpers from load_neon.py."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from load_neon import load_env, connect

sql = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
conn = connect(load_env()); cur = conn.cursor()
cur.execute(sql)
if cur.description:
    print(" | ".join(d[0] for d in cur.description))
    print("-" * 60)
    for row in cur.fetchall():
        print(" | ".join("" if v is None else str(v) for v in row))
conn.commit(); cur.close(); conn.close()
