"""
Ledger: the system's permanent memory and audit trail.

Today each review lives and dies inside one dashboard session. The ledger keeps
a permanent record in one SQLite file, so the system can answer: what has been
analysed, which version of which document, who decided what and when, and what
changed since last time.

What it records
  documents  each uploaded policy, by name and content hash. New content under
             the same name becomes the next version; identical content is
             recognised as already seen.
  runs       each analysis of a document, with the backend and model used.
  findings   every finding of a run, remembered with the section it came from.
  events     an append-only trail of actions (who, when, what). Each entry
             carries a hash of the one before it, so editing or deleting an old
             entry is detected by verify_chain().

Two-person approval (maker / checker)
  approve_first(...)  a first reviewer approves  -> "awaiting_second_approval"
  approve_final(...)  a DIFFERENT person confirms -> "approved"
  reject(...)         anyone rejects, with a note -> "rejected"

Memory across versions
  compare_versions()  which sections are unchanged / changed / added / removed
  prior_decisions()   what was decided before on a section whose text is unchanged

What it does not do: it does not verify who a person is. Names are recorded as
given. Real sign-in would be needed for that.

Not connected to the dashboard yet. The dashboard would call register_document()
on upload, record_run() after an analysis, and record_event()/approve_*() for
each decision. Run `python shared/ledger.py demo` to see it work on demo data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "ledger.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (name, sha256)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    created_at TEXT NOT NULL,
    backend TEXT,
    model TEXT
);
CREATE TABLE IF NOT EXISTS findings (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    finding_id TEXT NOT NULL,
    section_id TEXT,
    locator TEXT,
    requirement TEXT,
    framework_control TEXT,
    coverage TEXT,
    snapshot TEXT NOT NULL,
    PRIMARY KEY (run_id, finding_id)
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT,
    action TEXT NOT NULL,
    run_id INTEGER,
    finding_id TEXT,
    detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
"""


class ApprovalError(ValueError):
    """An approval step that the rules do not allow."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _chain_hash(prev: str, payload: dict) -> str:
    return hashlib.sha256((prev + _canon(payload)).encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv("LEDGER_PATH") or DEFAULT_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self, write: bool = False):
        # One short connection per operation, so separate sessions never share state in memory.
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")        # take the write lock before reading the last hash
            yield db
            if write:
                db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    # ---- the append-only trail ------------------------------------------------

    def _append(self, db, actor, action, run_id=None, finding_id=None, role=None, detail=None, ts=None) -> int:
        last = db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else GENESIS
        payload = {"ts": ts or _now(), "actor": actor, "role": role, "action": action,
                   "run_id": run_id, "finding_id": finding_id, "detail": detail or {}}
        cur = db.execute(
            "INSERT INTO events (ts, actor, role, action, run_id, finding_id, detail, prev_hash, hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (payload["ts"], actor, role, action, run_id, finding_id, _canon(payload["detail"]),
             prev, _chain_hash(prev, payload)))
        return cur.lastrowid

    def record_event(self, actor, action, run_id=None, finding_id=None, role=None, detail=None, ts=None) -> int:
        """Add one entry to the trail. Fields mirror the dashboard's AuditEvent."""
        if not actor or not str(actor).strip():
            raise ValueError("an event needs an actor (who did it)")
        with self._db(write=True) as db:
            return self._append(db, str(actor).strip(), action, run_id, finding_id, role, detail, ts)

    def verify_chain(self) -> dict:
        """Recompute every hash. ok=False means an entry was edited, removed or inserted."""
        with self._db() as db:
            rows = db.execute("SELECT * FROM events ORDER BY seq").fetchall()
        prev = GENESIS
        for row in rows:
            payload = {"ts": row["ts"], "actor": row["actor"], "role": row["role"], "action": row["action"],
                       "run_id": row["run_id"], "finding_id": row["finding_id"], "detail": json.loads(row["detail"])}
            if row["prev_hash"] != prev or row["hash"] != _chain_hash(prev, payload):
                return {"ok": False, "first_bad_seq": row["seq"], "events": len(rows)}
            prev = row["hash"]
        return {"ok": True, "first_bad_seq": None, "events": len(rows)}

    def events(self, document_name=None, run_id=None, finding_id=None) -> list[dict]:
        sql = ("SELECT e.*, d.name AS document, d.version AS version FROM events e "
               "LEFT JOIN runs r ON r.id = e.run_id LEFT JOIN documents d ON d.id = r.document_id WHERE 1=1")
        args: list = []
        for clause, value in ((" AND d.name = ?", document_name), (" AND e.run_id = ?", run_id),
                              (" AND e.finding_id = ?", finding_id)):
            if value is not None:
                sql += clause
                args.append(value)
        with self._db() as db:
            rows = db.execute(sql + " ORDER BY e.seq", args).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "actor": r["actor"], "role": r["role"], "action": r["action"],
                 "document": r["document"], "version": r["version"], "run_id": r["run_id"],
                 "finding_id": r["finding_id"], "detail": json.loads(r["detail"])} for r in rows]

    # ---- documents, runs, findings ---------------------------------------------

    def register_document(self, name: str, content) -> dict:
        """Note a document upload. Same name + same content = already seen; new content = next version."""
        data = content if isinstance(content, bytes) else str(content).encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM documents WHERE name = ? AND sha256 = ?", (name, sha)).fetchone()
            if row:
                return {"id": row["id"], "name": name, "version": row["version"], "sha256": sha,
                        "already_seen": True, "is_new_version": False, "previous_version": None}
            previous = db.execute("SELECT MAX(version) AS v FROM documents WHERE name = ?", (name,)).fetchone()["v"]
            version = (previous or 0) + 1
            cur = db.execute("INSERT INTO documents (name, sha256, version, created_at) VALUES (?,?,?,?)",
                             (name, sha, version, _now()))
            return {"id": cur.lastrowid, "name": name, "version": version, "sha256": sha,
                    "already_seen": False, "is_new_version": previous is not None, "previous_version": previous}

    def record_run(self, document_id: int, findings: list[dict], backend=None, model=None, actor="system",
                   detail=None) -> int:
        """Remember an analysis and all its findings. Returns the run id.

        detail: optional extra facts about the run (e.g. the sections analyzed), kept in its trail entry.
        """
        with self._db(write=True) as db:
            run_id = db.execute("INSERT INTO runs (document_id, created_at, backend, model) VALUES (?,?,?,?)",
                                (document_id, _now(), backend, model)).lastrowid
            for i, f in enumerate(findings):
                citation = f.get("citation") or {}
                db.execute(
                    "INSERT INTO findings (run_id, finding_id, section_id, locator, requirement, "
                    "framework_control, coverage, snapshot) VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, f.get("finding_id") or f"F{i + 1:03d}", citation.get("chunk_id"),
                     citation.get("locator"), f.get("requirement"), f.get("framework_control"),
                     f.get("coverage"), _canon(f)))
            self._append(db, actor, "analysis_run", run_id=run_id, role="system",
                         detail={**(detail or {}), "findings": len(findings), "backend": backend, "model": model})
            return run_id

    def latest_run(self, name: str, version=None):
        sql = ("SELECT r.id FROM runs r JOIN documents d ON d.id = r.document_id WHERE d.name = ?"
               + (" AND d.version = ?" if version is not None else "") + " ORDER BY r.id DESC LIMIT 1")
        with self._db() as db:
            row = db.execute(sql, (name, version) if version is not None else (name,)).fetchone()
        return row["id"] if row else None

    # ---- reading back (the dashboard's library, and reopening a stored review) ----

    def documents(self, name=None) -> list[dict]:
        """Every stored document version, oldest first."""
        sql = "SELECT * FROM documents" + (" WHERE name = ?" if name is not None else "") + " ORDER BY id"
        with self._db() as db:
            rows = db.execute(sql, (name,) if name is not None else ()).fetchall()
        return [{"id": r["id"], "name": r["name"], "version": r["version"], "sha256": r["sha256"],
                 "created_at": r["created_at"]} for r in rows]

    def runs(self, document_name=None) -> list[dict]:
        """Every analysis, oldest first, with its document's name and version."""
        sql = ("SELECT r.*, d.name AS document, d.version AS version, d.id AS doc_id FROM runs r "
               "JOIN documents d ON d.id = r.document_id"
               + (" WHERE d.name = ?" if document_name is not None else "") + " ORDER BY r.id")
        with self._db() as db:
            rows = db.execute(sql, (document_name,) if document_name is not None else ()).fetchall()
        return [{"id": r["id"], "document_id": r["doc_id"], "document": r["document"], "version": r["version"],
                 "created_at": r["created_at"], "backend": r["backend"], "model": r["model"]} for r in rows]

    def run_findings(self, run_id) -> list[dict]:
        """One run's findings exactly as they were recorded (the stored snapshots), in order."""
        with self._db() as db:
            rows = db.execute("SELECT snapshot FROM findings WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()
        return [json.loads(r["snapshot"]) for r in rows]

    def last_seq(self, run_id=None) -> int:
        """The newest trail entry, for one run or overall; 0 when there is none."""
        sql = "SELECT MAX(seq) AS s FROM events" + (" WHERE run_id = ?" if run_id is not None else "")
        with self._db() as db:
            row = db.execute(sql, (run_id,) if run_id is not None else ()).fetchone()
        return row["s"] or 0

    def _finding_exists(self, run_id, finding_id) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM findings WHERE run_id = ? AND finding_id = ?",
                              (run_id, finding_id)).fetchone() is not None

    # ---- two-person approval ---------------------------------------------------

    def finding_state(self, run_id: int, finding_id: str) -> dict:
        """pending -> awaiting_second_approval -> approved, or rejected. The latest decision wins.

        "reopened" (a decision undone, or sent back by the second reviewer) returns it to pending.
        """
        fresh = {"state": "pending", "first_approver": None, "final_approver": None, "rejected_by": None}
        state = dict(fresh)
        for e in self.events(run_id=run_id, finding_id=finding_id):
            if e["action"] == "approved_first":
                state.update(state="awaiting_second_approval", first_approver=e["actor"],
                             final_approver=None, rejected_by=None)
            elif e["action"] == "approved_final":
                state.update(state="approved", final_approver=e["actor"], rejected_by=None)
            elif e["action"] == "rejected":
                state.update(state="rejected", rejected_by=e["actor"])
            elif e["action"] == "reopened":
                state = dict(fresh)
        return state

    def approve_first(self, actor, run_id, finding_id, note=None) -> int:
        self._require_finding(run_id, finding_id)
        return self.record_event(actor, "approved_first", run_id, finding_id, "reviewer", {"note": note})

    def approve_final(self, actor, run_id, finding_id, note=None) -> int:
        self._require_finding(run_id, finding_id)
        state = self.finding_state(run_id, finding_id)
        if state["state"] != "awaiting_second_approval":
            raise ApprovalError(f"{finding_id} is '{state['state']}'; it needs a first approval before a second one")
        if str(actor).strip().lower() == str(state["first_approver"]).strip().lower():
            raise ApprovalError("a different person must give the second approval")
        return self.record_event(actor, "approved_final", run_id, finding_id, "approver", {"note": note})

    def reject(self, actor, run_id, finding_id, note) -> int:
        self._require_finding(run_id, finding_id)
        if not note or not str(note).strip():
            raise ApprovalError("a rejection needs a note")
        return self.record_event(actor, "rejected", run_id, finding_id, "reviewer", {"note": note})

    def _require_finding(self, run_id, finding_id):
        if not self._finding_exists(run_id, finding_id):
            raise ApprovalError(f"unknown finding {finding_id!r} in run {run_id}")

    # ---- memory across versions ------------------------------------------------

    def _sections(self, run_id) -> dict[str, str]:
        with self._db() as db:
            rows = db.execute("SELECT DISTINCT locator, section_id FROM findings WHERE run_id = ?", (run_id,)).fetchall()
        return {r["locator"]: r["section_id"] for r in rows if r["locator"]}

    def compare_versions(self, name: str, old_version: int, new_version: int) -> dict:
        """Which sections (that produced findings) are unchanged, changed, added or removed."""
        old_run, new_run = self.latest_run(name, old_version), self.latest_run(name, new_version)
        if old_run is None or new_run is None:
            raise ValueError("both versions need an analysis run to be compared")
        old, new = self._sections(old_run), self._sections(new_run)
        return {
            "unchanged": sorted(k for k in new if k in old and old[k] == new[k]),
            "changed": sorted(k for k in new if k in old and old[k] != new[k]),
            "added": sorted(k for k in new if k not in old),
            "removed": sorted(k for k in old if k not in new),
        }

    def prior_decisions(self, name: str, section_ids: list[str], before_run_id=None) -> dict[str, list[dict]]:
        """For sections whose text is unchanged: what was decided last time, and by whom."""
        out: dict[str, list[dict]] = {}
        for section_id in section_ids:
            sql = ("SELECT f.run_id FROM findings f JOIN runs r ON r.id = f.run_id "
                   "JOIN documents d ON d.id = r.document_id WHERE d.name = ? AND f.section_id = ?"
                   + (" AND f.run_id < ?" if before_run_id is not None else "") + " ORDER BY f.run_id DESC LIMIT 1")
            args = (name, section_id) + ((before_run_id,) if before_run_id is not None else ())
            with self._db() as db:
                row = db.execute(sql, args).fetchone()
                if not row:
                    continue
                found = db.execute("SELECT finding_id, requirement, framework_control FROM findings "
                                   "WHERE run_id = ? AND section_id = ? ORDER BY finding_id",
                                   (row["run_id"], section_id)).fetchall()
            out[section_id] = [{"run_id": row["run_id"], "finding_id": f["finding_id"],
                                "requirement": f["requirement"], "control": f["framework_control"],
                                **self.finding_state(row["run_id"], f["finding_id"])} for f in found]
        return out

    def memory_note(self, name: str) -> str:
        """A readable summary of what the system knows about one document (an Obsidian-style note)."""
        with self._db() as db:
            docs = db.execute("SELECT version, created_at FROM documents WHERE name = ? ORDER BY version", (name,)).fetchall()
        if not docs:
            return f"# {name}\n\nNothing recorded for this document yet.\n"
        lines = [f"# {name}", "", "## Versions"]
        lines += [f"- v{d['version']}, first seen {d['created_at']}" for d in docs]
        run_id = self.latest_run(name)
        if run_id is not None:
            with self._db() as db:
                findings = db.execute("SELECT finding_id, requirement, locator, coverage FROM findings "
                                      "WHERE run_id = ? ORDER BY finding_id", (run_id,)).fetchall()
            states = {f["finding_id"]: self.finding_state(run_id, f["finding_id"])["state"] for f in findings}
            tally: dict[str, int] = {}
            for s in states.values():
                tally[s] = tally.get(s, 0) + 1
            lines += ["", f"## Latest analysis (run {run_id})",
                      f"{len(findings)} findings: " + ", ".join(f"{n} {s}" for s, n in sorted(tally.items()))]
            open_items = [f for f in findings if states[f["finding_id"]] in ("pending", "awaiting_second_approval")]
            if open_items:
                lines += ["", "## Still open"]
                lines += [f"- {f['finding_id']} {f['requirement']} ({f['locator']}), {states[f['finding_id']]}"
                          for f in open_items]
        recent = self.events(document_name=name)[-5:]
        if recent:
            lines += ["", "## Recent activity"]
            lines += [f"- {e['ts']} {e['actor']}: {e['action']}" + (f" {e['finding_id']}" if e["finding_id"] else "")
                      for e in recent]
        return "\n".join(lines) + "\n"

    def write_memory_note(self, name: str, directory) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (re.sub(r"[^A-Za-z0-9._-]+", "_", name) + ".md")
        path.write_text(self.memory_note(name), encoding="utf-8")
        return path


# ---- command line ------------------------------------------------------------

def _print_history(ledger: Ledger, name=None):
    rows = ledger.events(document_name=name)
    if not rows:
        print("No events recorded yet.")
    for e in rows:
        doc = f"{e['document']} v{e['version']}" if e["document"] else "-"
        print(f"{e['seq']:>3}  {e['ts']}  {e['actor']:<10} {e['action']:<15} {e['finding_id'] or '':<6} {doc}")
    check = ledger.verify_chain()
    print("\nTrail integrity:", "OK" if check["ok"] else f"BROKEN at entry {check['first_bad_seq']}", f"({check['events']} entries)")


def _demo():
    import tempfile
    print("DEMO DATA (invented, not real analysis results)\n")
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(Path(tmp) / "demo.sqlite3")
        findings = [
            {"finding_id": "F001", "requirement": "Use multifactor authentication", "coverage": "Partial",
             "framework_control": "IA-2", "citation": {"chunk_id": "sec-3.5-v1", "locator": "Section 3.5 Authentication"}},
            {"finding_id": "F002", "requirement": "Lock unattended screens", "coverage": "Partial",
             "framework_control": "AC-11", "citation": {"chunk_id": "sec-3.2", "locator": "Section 3.2 Accounts"}},
        ]
        v1 = ledger.register_document("Computer_Security_Policy.pdf", "policy text, first draft")
        run1 = ledger.record_run(v1["id"], findings, backend="demo", model="demo")
        print(f"1. Uploaded {v1['name']} -> version {v1['version']}; analysis run {run1} recorded")
        print("   same file uploaded again ->", "recognised, already seen" if
              ledger.register_document("Computer_Security_Policy.pdf", "policy text, first draft")["already_seen"] else "NEW?")
        ledger.approve_first("Rashmi", run1, "F001", "MFA gap is real")
        try:
            ledger.approve_final("Rashmi", run1, "F001")
        except ApprovalError as exc:
            print(f"2. Rashmi tries to give the second approval herself -> refused: {exc}")
        ledger.approve_final("Anu", run1, "F001")
        ledger.reject("Mahsa", run1, "F002", "control does not fit this sentence")
        for fid in ("F001", "F002"):
            s = ledger.finding_state(run1, fid)
            print(f"3. {fid}: {s['state']} (first: {s['first_approver']}, second: {s['final_approver']}, rejected by: {s['rejected_by']})")
        changed = [dict(findings[0], citation={"chunk_id": "sec-3.5-v2", "locator": "Section 3.5 Authentication"}), findings[1]]
        v2 = ledger.register_document("Computer_Security_Policy.pdf", "policy text, revised draft")
        run2 = ledger.record_run(v2["id"], changed, backend="demo", model="demo")
        print(f"4. Revised policy uploaded -> version {v2['version']} (previous: v{v2['previous_version']})")
        print("   changes:", ledger.compare_versions("Computer_Security_Policy.pdf", 1, 2))
        old = ledger.prior_decisions("Computer_Security_Policy.pdf", ["sec-3.2", "sec-3.5-v2"], before_run_id=run2)
        for sid, items in old.items():
            print(f"   earlier decision on unchanged section {sid}:", [(i['requirement'], i['state']) for i in items])
        print("\n--- history ---")
        _print_history(ledger, "Computer_Security_Policy.pdf")
        with sqlite3.connect(ledger.path) as db:                      # someone edits history
            db.execute("UPDATE events SET actor = 'Someone Else' WHERE seq = 3")
        print("\nAfter secretly changing who approved entry 3:")
        print("Trail integrity:", ledger.verify_chain())


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "demo":
        _demo()
    elif command == "history":
        _print_history(Ledger(), sys.argv[2] if len(sys.argv) > 2 else None)
    elif command == "verify":
        print(Ledger().verify_chain())
    else:
        sys.exit("usage: python shared/ledger.py demo | history [document name] | verify")
