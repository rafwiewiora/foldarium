#!/usr/bin/env python
"""Fetch RCSB initial_release_date for all target PDB ids (first 4 chars of target_system).
Cache to JSON. Batched via GraphQL. For cross-check vs parquet's target_release_date."""
import json, time, sys
import pandas as pd
import urllib.request

CACHE = "/Users/rafalwiewiora/rnp_data/rcsb_release_dates.json"
URL = "https://data.rcsb.org/graphql"

df = pd.read_parquet('/tmp/rnp_allsim.parquet', columns=['target_system'])
pdbs = sorted(set(df['target_system'].str[:4].str.upper().unique()))
print(f"distinct target PDBs: {len(pdbs)}", flush=True)

try:
    cache = json.load(open(CACHE))
except Exception:
    cache = {}
print(f"cache preload: {len(cache)}", flush=True)

todo = [p for p in pdbs if p not in cache]
print(f"to fetch: {len(todo)}", flush=True)

def fetch(batch):
    ids = ",".join(f'"{p}"' for p in batch)
    q = f'{{entries(entry_ids:[{ids}]){{rcsb_id rcsb_accession_info{{initial_release_date}}}}}}'
    body = json.dumps({"query": q}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:
            print(f"  retry {attempt}: {e}", flush=True)
            time.sleep(3 * (attempt + 1))
    return None

BATCH = 500
for i in range(0, len(todo), BATCH):
    batch = todo[i:i+BATCH]
    res = fetch(batch)
    got = set()
    if res and res.get("data", {}).get("entries"):
        for e in res["data"]["entries"]:
            if e is None:
                continue
            rid = e["rcsb_id"]
            d = (e.get("rcsb_accession_info") or {}).get("initial_release_date")
            cache[rid] = d
            got.add(rid)
    # mark not-found as None
    for p in batch:
        if p not in got and p not in cache:
            cache[p] = None
    if i % 5000 == 0:
        json.dump(cache, open(CACHE, "w"))
        print(f"  progress {i+len(batch)}/{len(todo)} cached={len(cache)}", flush=True)
    time.sleep(0.1)

json.dump(cache, open(CACHE, "w"))
found = sum(1 for v in cache.values() if v)
missing = sum(1 for v in cache.values() if not v)
print(f"DONE. cached={len(cache)} found={found} missing={missing}", flush=True)
