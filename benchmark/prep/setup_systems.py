"""Assemble a few real example systems for the Mol* viewer.

Reads the seeded VERDICT DB (real AlphaFold3 poses on prospective CAMEO targets), copies the chosen
targets' model CIFs into viewer/systems/<id>/, and writes systems.json — the manifest the viewer reads.
Picked for variety: a nailed case, a mostly-right case, a small ligand, and an all-wrong 'confident fake'.
"""
import sqlite3, shutil, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE.parent / "verdict" / "data" / "verdict.db"
POSES = HERE.parent / "verdict" / "data" / "poses"
OUT = HERE / "systems"

PICK = {
    "11UC": "AF3 nails it — all 5 poses sub-1Å",
    "10LW": "mostly right — 4/5 correct, one stray pose",
    "21KW": "small fragment, tightly placed",
    "13JK": "confident fake — all 5 poses ~15Å wrong",
}

con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
systems = []
for tgt, blurb in PICK.items():
    lig = con.execute(
        "SELECT l.* FROM ligand l JOIN pose p ON p.ligand_id=l.id GROUP BY l.id "
        "ORDER BY COUNT(p.id) DESC LIMIT 1", ()).execute if False else None
    lrow = con.execute("SELECT * FROM ligand WHERE target_id=? LIMIT 1", (tgt,)).fetchone()
    poses = con.execute(
        "SELECT p.sample, p.confidence, p.coord_path, s.rmsd, s.correct_2a "
        "FROM pose p LEFT JOIN score s ON s.pose_id=p.id "
        "WHERE p.ligand_id=? ORDER BY p.sample", (lrow["id"],)).fetchall()
    sysdir = OUT / tgt
    sysdir.mkdir(parents=True, exist_ok=True)
    pose_list = []
    for p in poses:
        src = POSES / p["coord_path"]
        fname = f"model-{p['sample']}.cif"
        if src.exists():
            shutil.copy(src, sysdir / fname)
        pose_list.append({
            "sample": p["sample"], "file": f"systems/{tgt}/{fname}",
            "confidence": round(p["confidence"], 1) if p["confidence"] is not None else None,
            "rmsd": round(p["rmsd"], 2) if p["rmsd"] is not None else None,
            "correct": bool(p["correct_2a"]),
        })
    systems.append({
        "id": tgt, "blurb": blurb,
        "ligand": lrow["het_code"], "n_atoms": lrow["n_atoms"],
        "model": "AlphaFold3", "poses": pose_list,
    })

(HERE / "systems.json").write_text(json.dumps({"systems": systems}, indent=2))
print(f"wrote {len(systems)} systems -> {HERE/'systems.json'}")
for s in systems:
    print(f"  {s['id']} ({s['ligand']}): {len(s['poses'])} poses — {s['blurb']}")
