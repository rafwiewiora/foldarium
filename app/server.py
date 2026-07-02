"""Pose-quiz server: serves the static app + a leaderboard backed by SQLite. Sessions are stored by
item-ID with per-pose results, so the item pool can GROW over time without breaking past sessions
(accuracy is always computed over whatever items a session actually answered).

Run:  python3 server.py [PORT]   (default 8791)   then open http://localhost:8791
Data: quiz.db (SQLite) created alongside this file.

Endpoints (JSON):
  POST /api/session   {username, answers:[{item_id, ligand, picked_sample, picked_correct, picked_rmsd,
                       af3_pick_sample, af3_correct, n_clusters, ts}], client_ts}
  GET  /api/leaderboard            -> aggregated per-username stats
  GET  /api/session/<id>           -> one session's answers
  everything else -> static files from this directory
"""
import json, sqlite3, sys, time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "quiz.db"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        created REAL NOT NULL,
        n_items INTEGER, n_correct INTEGER, n_af3_correct INTEGER);
    CREATE TABLE IF NOT EXISTS answers(
        session_id INTEGER NOT NULL,
        item_id TEXT NOT NULL,
        ligand TEXT,
        picked_sample INTEGER,
        picked_correct INTEGER,
        picked_rmsd REAL,
        af3_pick_sample INTEGER,
        af3_correct INTEGER,
        n_clusters INTEGER,
        ts REAL,
        FOREIGN KEY(session_id) REFERENCES sessions(id));
    CREATE INDEX IF NOT EXISTS idx_ans_session ON answers(session_id);
    CREATE INDEX IF NOT EXISTS idx_ans_user ON answers(item_id);
    """)
    c.commit(); c.close()


def save_session(p):
    answers = p.get("answers", [])
    user = (p.get("username") or "anon").strip()[:40] or "anon"
    nc = sum(1 for a in answers if a.get("picked_correct"))
    naf3 = sum(1 for a in answers if a.get("af3_correct"))
    c = db()
    cur = c.execute("INSERT INTO sessions(username,created,n_items,n_correct,n_af3_correct) VALUES(?,?,?,?,?)",
                    (user, time.time(), len(answers), nc, naf3))
    sid = cur.lastrowid
    for a in answers:
        c.execute("""INSERT INTO answers(session_id,item_id,ligand,picked_sample,picked_correct,picked_rmsd,
                     af3_pick_sample,af3_correct,n_clusters,ts) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (sid, a.get("item_id"), a.get("ligand"), a.get("picked_sample"),
                   int(bool(a.get("picked_correct"))), a.get("picked_rmsd"), a.get("af3_pick_sample"),
                   int(bool(a.get("af3_correct"))), a.get("n_clusters"), a.get("ts")))
    c.commit(); c.close()
    return {"session_id": sid, "n_items": len(answers), "n_correct": nc, "n_af3_correct": naf3}


def leaderboard():
    # Aggregate over ALL answers per username (so re-playing / newly-added items just accumulate).
    # DISTINCT item per user is counted once (latest answer) so the pool growing is handled cleanly.
    c = db()
    rows = c.execute("""
      WITH latest AS (
        SELECT s.username AS username, a.item_id AS item_id, a.picked_correct AS pc, a.af3_correct AS ac,
               ROW_NUMBER() OVER (PARTITION BY s.username, a.item_id ORDER BY a.ts DESC) AS rn
        FROM answers a JOIN sessions s ON a.session_id=s.id)
      SELECT username, COUNT(*) AS n, SUM(pc) AS correct, SUM(ac) AS af3_correct
      FROM latest WHERE rn=1 GROUP BY username ORDER BY (1.0*SUM(pc)/COUNT(*)) DESC, n DESC""").fetchall()
    sess = {r["username"]: r["s"] for r in c.execute(
        "SELECT username, COUNT(*) AS s FROM sessions GROUP BY username")}
    c.close()
    out = []
    for r in rows:
        n = r["n"]; cr = r["correct"] or 0; af = r["af3_correct"] or 0
        out.append({"username": r["username"], "items": n, "correct": cr,
                    "accuracy": round(100 * cr / n) if n else 0,
                    "af3_accuracy": round(100 * af / n) if n else 0,
                    "beat_af3_by": round(100 * (cr - af) / n) if n else 0,
                    "sessions": sess.get(r["username"], 1)})
    return out


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(HERE), **k)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/api/leaderboard":
            return self._json(leaderboard())
        if self.path.startswith("/api/session/"):
            sid = self.path.rsplit("/", 1)[-1]
            c = db(); ans = [dict(r) for r in c.execute("SELECT * FROM answers WHERE session_id=?", (sid,))]
            s = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone(); c.close()
            return self._json({"session": dict(s) if s else None, "answers": ans})
        return super().do_GET()

    def do_POST(self):
        if self.path.rstrip("/") == "/api/session":
            n = int(self.headers.get("Content-Length", 0))
            try:
                p = json.loads(self.rfile.read(n) or b"{}")
                return self._json(save_session(p))
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        self.send_error(404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    init_db()
    print(f"pose-quiz + leaderboard on http://localhost:{PORT}  (db: {DB.name})")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
