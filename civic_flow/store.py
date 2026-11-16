"""管理 SQLite 事务、审计日志与持久化状态。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .contracts import DomainError

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS grids (
    grid_id TEXT PRIMARY KEY, district TEXT NOT NULL, active INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS route_rules (
    category TEXT PRIMARY KEY, department TEXT NOT NULL, priority INTEGER NOT NULL,
    response_hours INTEGER NOT NULL, resolution_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS batches (
    batch_key TEXT PRIMARY KEY, digest TEXT NOT NULL, submitted_at TEXT NOT NULL,
    imported_count INTEGER NOT NULL, sequence_start INTEGER NOT NULL,
    sequence_end INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    case_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, external_id TEXT NOT NULL,
    grid_id TEXT NOT NULL REFERENCES grids(grid_id), district TEXT NOT NULL,
    category TEXT NOT NULL, summary TEXT NOT NULL, resident_hash TEXT NOT NULL,
    priority INTEGER NOT NULL, accepted_at TEXT NOT NULL,
    department TEXT NOT NULL, initial_department TEXT NOT NULL, route_revision INTEGER NOT NULL,
    response_due TEXT NOT NULL, resolution_due TEXT NOT NULL,
    status TEXT NOT NULL, resolution TEXT,
    first_response_at TEXT, last_event_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, created_sequence INTEGER NOT NULL,
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS cases_grid_status ON cases(grid_id,status);
CREATE INDEX IF NOT EXISTS cases_category_status ON cases(category,status);
CREATE INDEX IF NOT EXISTS cases_accepted ON cases(accepted_at);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(case_id),
    event_key TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL, department TEXT, note TEXT, resolution TEXT,
    case_version INTEGER NOT NULL, event_sequence INTEGER NOT NULL UNIQUE,
    digest TEXT NOT NULL, UNIQUE(case_id,event_key), UNIQUE(case_id,case_version)
);
CREATE INDEX IF NOT EXISTS events_case_time ON events(case_id,occurred_at,event_sequence);
CREATE TABLE IF NOT EXISTS journal (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT NOT NULL,
    entity TEXT NOT NULL, entity_id TEXT NOT NULL, payload_digest TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    report_key TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
    as_of TEXT NOT NULL, watermark INTEGER NOT NULL,
    group_by TEXT NOT NULL, district TEXT, category TEXT,
    body_json TEXT NOT NULL, grouping_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
    item_key TEXT NOT NULL, revision INTEGER NOT NULL,
    old_json TEXT, new_json TEXT NOT NULL, sequence INTEGER NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS problems (
    problem_id INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_id TEXT NOT NULL REFERENCES grids(grid_id), district TEXT NOT NULL,
    category TEXT NOT NULL, department TEXT NOT NULL, title TEXT NOT NULL,
    created_at TEXT NOT NULL, created_sequence INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS problems_created ON problems(created_sequence);
CREATE TABLE IF NOT EXISTS problem_members (
    member_id INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id INTEGER NOT NULL REFERENCES problems(problem_id),
    case_id INTEGER NOT NULL REFERENCES cases(case_id),
    joined_at TEXT NOT NULL, joined_sequence INTEGER NOT NULL,
    left_at TEXT, left_sequence INTEGER, origin TEXT NOT NULL,
    origin_actor TEXT NOT NULL DEFAULT 'system',
    UNIQUE(case_id, joined_sequence)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_membership
    ON problem_members(case_id) WHERE left_sequence IS NULL;
CREATE INDEX IF NOT EXISTS members_problem ON problem_members(problem_id, joined_sequence);
CREATE TABLE IF NOT EXISTS association_suggestions (
    suggestion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_low_id INTEGER NOT NULL REFERENCES cases(case_id),
    case_high_id INTEGER NOT NULL REFERENCES cases(case_id),
    reason TEXT NOT NULL, similarity REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL, created_sequence INTEGER NOT NULL,
    resolved_at TEXT, resolved_sequence INTEGER,
    confirmed_problem_id INTEGER REFERENCES problems(problem_id),
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(case_low_id, case_high_id),
    CHECK(case_low_id < case_high_id)
);
CREATE INDEX IF NOT EXISTS suggestions_status ON association_suggestions(status, suggestion_id);
CREATE TABLE IF NOT EXISTS problem_merges (
    merge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    survivor_problem_id INTEGER NOT NULL REFERENCES problems(problem_id),
    merged_problem_id INTEGER NOT NULL REFERENCES problems(problem_id),
    suggestion_id INTEGER REFERENCES association_suggestions(suggestion_id),
    actor TEXT NOT NULL, occurred_at TEXT NOT NULL, merge_sequence INTEGER NOT NULL,
    reversed_at TEXT, reversed_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS merge_moves (
    merge_id INTEGER NOT NULL REFERENCES problem_merges(merge_id),
    case_id INTEGER NOT NULL REFERENCES cases(case_id),
    from_problem_id INTEGER NOT NULL, to_problem_id INTEGER NOT NULL,
    PRIMARY KEY(merge_id, case_id)
);
CREATE INDEX IF NOT EXISTS merges_survivor ON problem_merges(survivor_problem_id, merge_id);
CREATE TABLE IF NOT EXISTS problem_operations (
    operation_key TEXT PRIMARY KEY, operation TEXT NOT NULL, digest TEXT NOT NULL,
    result_json TEXT NOT NULL, occurred_at TEXT NOT NULL
);
"""

def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()

def row(value: sqlite3.Row | None) -> dict | None:
    return dict(value) if value is not None else None

class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(SCHEMA)
            self._migrate(db)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Add columns introduced after a database was first created."""
        additions = {
            "reports": {"grouping_json": "TEXT NOT NULL DEFAULT '{}'"},
            "problem_members": {"origin_actor": "TEXT NOT NULL DEFAULT 'system'"},
        }
        for table, columns in additions.items():
            existing = {item["name"] for item in db.execute("PRAGMA table_info(" + table + ")")}
            for name, declaration in columns.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        if self.path != ":memory:":
            db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def journal(db: sqlite3.Connection, operation: str, entity: str, entity_id: str,
                payload: object, occurred_at: str) -> int:
        cursor = db.execute(
            "INSERT INTO journal(operation,entity,entity_id,payload_digest,occurred_at) VALUES(?,?,?,?,?)",
            (operation, entity, entity_id, digest(payload), occurred_at),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def watermark(db: sqlite3.Connection) -> int:
        return int(db.execute("SELECT COALESCE(MAX(sequence),0) FROM journal").fetchone()[0])

    @staticmethod
    def grid(db: sqlite3.Connection, grid_id: str) -> dict | None:
        return row(db.execute("SELECT * FROM grids WHERE grid_id=?", (grid_id,)).fetchone())

    @staticmethod
    def rule(db: sqlite3.Connection, category: str) -> dict | None:
        return row(db.execute("SELECT * FROM route_rules WHERE category=?", (category,)).fetchone())

    @staticmethod
    def case(db: sqlite3.Connection, case_id: int) -> dict:
        result = row(db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone())
        if result is None:
            raise DomainError("CASE_NOT_FOUND", "Case does not exist", 404)
        return result

    @staticmethod
    def case_by_external(db: sqlite3.Connection, source: str, external_id: str) -> dict | None:
        return row(db.execute("SELECT * FROM cases WHERE source=? AND external_id=?",
                              (source, external_id)).fetchone())

    @staticmethod
    def batch(db: sqlite3.Connection, batch_key: str) -> dict | None:
        return row(db.execute("SELECT * FROM batches WHERE batch_key=?", (batch_key,)).fetchone())

    @staticmethod
    def report(db: sqlite3.Connection, report_key: str) -> dict | None:
        return row(db.execute("SELECT * FROM reports WHERE report_key=?", (report_key,)).fetchone())

    @staticmethod
    def event_by_key(db: sqlite3.Connection, case_id: int, event_key: str) -> dict | None:
        return row(db.execute("SELECT * FROM events WHERE case_id=? AND event_key=?",
                              (case_id, event_key)).fetchone())

    @staticmethod
    def events(db: sqlite3.Connection, case_id: int, watermark: int | None = None) -> list[dict]:
        if watermark is None:
            sql, params = "SELECT * FROM events WHERE case_id=? ORDER BY case_version", (case_id,)
        else:
            sql = "SELECT * FROM events WHERE case_id=? AND event_sequence<=? ORDER BY case_version"
            params = (case_id, watermark)
        return [dict(item) for item in db.execute(sql, params)]

    @staticmethod
    def changes(db: sqlite3.Connection, after: int = 0, limit: int = 100) -> list[dict]:
        if not 0 <= after or not 1 <= limit <= 500:
            raise DomainError("BAD_CURSOR", "Invalid journal cursor or limit")
        return [dict(item) for item in db.execute(
            "SELECT * FROM journal WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit))]

    @staticmethod
    def policy_history(db: sqlite3.Connection, kind: str, item_key: str) -> list[dict]:
        return [dict(item) for item in db.execute(
            "SELECT * FROM policy_changes WHERE kind=? AND item_key=? ORDER BY revision",
            (kind, item_key))]

    @staticmethod
    def case_list(db: sqlite3.Connection, *, district: str | None = None,
                  category: str | None = None, status: str | None = None,
                  after: int = 0, limit: int = 50) -> list[dict]:
        if not 1 <= limit <= 200 or after < 0:
            raise DomainError("BAD_PAGE", "Invalid pagination")
        clauses = ["case_id > ?"]
        params: list[object] = [after]
        for field, value in (("district", district), ("category", category), ("status", status)):
            if value is not None:
                clauses.append(field + " = ?")
                params.append(value)
        params.append(limit)
        sql = "SELECT * FROM cases WHERE " + " AND ".join(clauses) + " ORDER BY case_id LIMIT ?"
        return [dict(item) for item in db.execute(sql, params)]

    @staticmethod
    def require_report(db: sqlite3.Connection, report_key: str) -> dict:
        report = Store.report(db, report_key)
        if report is None:
            raise DomainError("REPORT_NOT_FOUND", "Report does not exist", 404)
        return report

    @staticmethod
    def save_policy_change(db: sqlite3.Connection, kind: str, item_key: str,
                           revision: int, old: dict | None, new: dict, sequence: int) -> None:
        db.execute("INSERT INTO policy_changes(kind,item_key,revision,old_json,new_json,sequence) "
                   "VALUES(?,?,?,?,?,?)",
                   (kind, item_key, revision, canonical(old) if old else None,
                    canonical(new), sequence))

    @staticmethod
    def table_counts(db: sqlite3.Connection) -> dict:
        return {name: int(db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0])
                for name in ("grids", "route_rules", "batches", "cases", "events",
                             "reports", "problems", "association_suggestions")}

    @staticmethod
    def journal_span(db: sqlite3.Connection, start: int, end: int) -> list[dict]:
        if start < 0 or end < start:
            raise DomainError("BAD_SPAN", "Invalid journal span")
        return [dict(item) for item in db.execute(
            "SELECT * FROM journal WHERE sequence BETWEEN ? AND ? ORDER BY sequence",
            (start, end))]

    @staticmethod
    def batch_cases(db: sqlite3.Connection, batch_key: str) -> list[dict]:
        batch = Store.batch(db, batch_key)
        if batch is None:
            raise DomainError("BATCH_NOT_FOUND", "Batch does not exist", 404)
        return [dict(item) for item in db.execute(
            "SELECT case_id,source,external_id,grid_id,category,accepted_at,created_sequence "
            "FROM cases WHERE created_sequence BETWEEN ? AND ? ORDER BY created_sequence",
            (batch["sequence_start"], batch["sequence_end"]))]

    @staticmethod
    def problem(db: sqlite3.Connection, problem_id: int) -> dict:
        result = row(db.execute("SELECT * FROM problems WHERE problem_id=?",
                                (problem_id,)).fetchone())
        if result is None:
            raise DomainError("PROBLEM_NOT_FOUND", "Problem does not exist", 404)
        return result

    @staticmethod
    def optional_problem(db: sqlite3.Connection, problem_id: int) -> dict | None:
        return row(db.execute("SELECT * FROM problems WHERE problem_id=?",
                              (problem_id,)).fetchone())

    @staticmethod
    def memberships(db: sqlite3.Connection, watermark: int | None = None,
                    stamp: str | None = None) -> list[dict]:
        """Membership intervals visible at a watermark and business time.

        Rows are never deleted: a split, merge or its reversal closes the old
        interval and appends a new one.  An interval is counted when its join
        is visible at the watermark and happened by ``stamp``; it stops
        counting only when both its end event is visible at the watermark and
        its business time is by ``stamp``.  A report frozen at an older
        watermark therefore keeps exactly the grouping it was created with,
        while ``as_of`` recomputation sees the grouping of that moment.
        """
        if watermark is None:
            watermark = Store.watermark(db)
        params: list[object] = [watermark, watermark]
        if stamp is None:
            sql = ("SELECT * FROM problem_members WHERE joined_sequence<=? "
                   "AND (left_sequence IS NULL OR left_sequence>?)")
        else:
            # Rebuild for business time ``stamp`` using only what was known at
            # ``watermark``: a leave that happened after ``stamp`` had not
            # happened yet, even if its end event is already visible now.
            sql = ("SELECT * FROM problem_members WHERE joined_sequence<=? "
                   "AND (left_sequence IS NULL OR left_sequence>? OR left_at>?) "
                   "AND joined_at<=?")
            params.extend([stamp, stamp])
        sql += " ORDER BY case_id, joined_sequence, member_id"
        return [dict(item) for item in db.execute(sql, params)]

    @staticmethod
    def membership_state(db: sqlite3.Connection, watermark: int | None = None,
                         stamp: str | None = None) -> dict[int, dict]:
        state: dict[int, dict] = {}
        for item in Store.memberships(db, watermark, stamp):
            state[int(item["case_id"])] = item
        return state

    @staticmethod
    def merge_rows(db: sqlite3.Connection, watermark: int | None = None,
                   stamp: str | None = None) -> list[dict]:
        if watermark is None:
            watermark = Store.watermark(db)
        sql = "SELECT * FROM problem_merges WHERE merge_sequence<=?"
        params: list[object] = [watermark]
        if stamp is not None:
            sql += " AND occurred_at<=?"
            params.append(stamp)
        sql += " ORDER BY merge_sequence, merge_id"
        return [dict(item) for item in db.execute(sql, params)]

    @staticmethod
    def merge_state(db: sqlite3.Connection, watermark: int | None = None,
                    stamp: str | None = None) -> dict[int, int]:
        """problem_id -> current survivor at a watermark and business time."""
        if watermark is None:
            watermark = Store.watermark(db)
        state: dict[int, int] = {}
        for item in Store.merge_rows(db, watermark, stamp):
            reversed_visible = (item["reversed_sequence"] is not None
                                and item["reversed_sequence"] <= watermark
                                and (stamp is None or item["reversed_at"] <= stamp))
            if reversed_visible:
                state.pop(int(item["merged_problem_id"]), None)
            else:
                state[int(item["merged_problem_id"])] = int(item["survivor_problem_id"])
        result: dict[int, int] = {}
        for merged, survivor in state.items():
            seen = {merged}
            while survivor in state and survivor not in seen:
                seen.add(survivor)
                survivor = state[survivor]
            result[merged] = survivor
        return result

    @staticmethod
    def active_problem_ids(db: sqlite3.Connection, watermark: int | None = None,
                           stamp: str | None = None) -> set[int]:
        if watermark is None:
            watermark = Store.watermark(db)
        merged = Store.merge_state(db, watermark, stamp)
        sql = "SELECT problem_id,created_at FROM problems WHERE created_sequence<=?"
        params: list[object] = [watermark]
        if stamp is not None:
            sql += " AND created_at<=?"
            params.append(stamp)
        return {int(item[0]) for item in db.execute(sql, params)
                if int(item[0]) not in merged}

    @staticmethod
    def problem_members(db: sqlite3.Connection, problem_id: int,
                        watermark: int | None = None,
                        stamp: str | None = None) -> list[dict]:
        return [item for item in Store.memberships(db, watermark, stamp)
                if int(item["problem_id"]) == problem_id]

    @staticmethod
    def current_membership(db: sqlite3.Connection, case_id: int) -> dict | None:
        return row(db.execute(
            "SELECT * FROM problem_members WHERE case_id=? AND left_sequence IS NULL",
            (case_id,)).fetchone())

    @staticmethod
    def suggestion(db: sqlite3.Connection, suggestion_id: int) -> dict:
        result = row(db.execute("SELECT * FROM association_suggestions WHERE suggestion_id=?",
                                (suggestion_id,)).fetchone())
        if result is None:
            raise DomainError("ASSOCIATION_NOT_FOUND", "Association suggestion does not exist", 404)
        return result

    @staticmethod
    def suggestion_by_pair(db: sqlite3.Connection, low_id: int, high_id: int,
                           reason: str | None = None) -> dict | None:
        """At most one suggestion exists per unordered case pair."""
        return row(db.execute(
            "SELECT * FROM association_suggestions WHERE case_low_id=? AND case_high_id=?",
            (low_id, high_id)).fetchone())

    @staticmethod
    def merge(db: sqlite3.Connection, merge_id: int) -> dict:
        result = row(db.execute("SELECT * FROM problem_merges WHERE merge_id=?",
                                (merge_id,)).fetchone())
        if result is None:
            raise DomainError("MERGE_NOT_FOUND", "Merge does not exist", 404)
        return result

    @staticmethod
    def journal_entities(db: sqlite3.Connection, entity: str, entity_id: str) -> list[dict]:
        return [dict(item) for item in db.execute(
            "SELECT * FROM journal WHERE entity=? AND entity_id=? ORDER BY sequence",
            (entity, entity_id))]


