"""配置目录的快照与变更影响查询。"""
from __future__ import annotations

from datetime import datetime

from .contracts import DomainError
from .store import Store

class CatalogService:
    def __init__(self, store: Store):
        self.store = store

    def overview(self) -> dict:
        with self.store.transaction() as db:
            grids = [dict(row) for row in db.execute("SELECT * FROM grids ORDER BY district,grid_id")]
            rules = [dict(row) for row in db.execute("SELECT * FROM route_rules ORDER BY category")]
            district_counts: dict[str, dict] = {}
            for grid in grids:
                entry = district_counts.setdefault(grid["district"], {"active_grids": 0,
                                                                        "inactive_grids": 0})
                entry["active_grids" if grid["active"] else "inactive_grids"] += 1
            return {"watermark": self.store.watermark(db), "grids": grids, "routes": rules,
                    "districts": [{"district": key, **value}
                                  for key, value in sorted(district_counts.items())]}

    def unconfigured(self) -> dict:
        with self.store.transaction() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT grid_id,district,active FROM grids ORDER BY grid_id")]
            categories = [dict(row) for row in db.execute(
                "SELECT category,department FROM route_rules ORDER BY category")]
            unused = [row for row in rows if not db.execute(
                "SELECT 1 FROM cases WHERE grid_id=? LIMIT 1", (row["grid_id"],)).fetchone()]
            unused_routes = [row for row in categories if not db.execute(
                "SELECT 1 FROM cases WHERE category=? LIMIT 1", (row["category"],)).fetchone()]
            return {"watermark": self.store.watermark(db), "grids_without_cases": unused,
                    "routes_without_cases": unused_routes}

    def impact(self, *, kind: str, key: str) -> dict:
        if kind not in {"grid", "route"}:
            raise DomainError("BAD_POLICY", "Unknown policy kind")
        with self.store.transaction() as db:
            if kind == "grid":
                policy = self.store.grid(db, key)
                field = "grid_id"
            else:
                policy = self.store.rule(db, key)
                field = "category"
            if policy is None:
                raise DomainError("POLICY_NOT_FOUND", "Policy does not exist", 404)
            cases = [dict(row) for row in db.execute(
                "SELECT case_id,status,route_revision,department,district,accepted_at "
                "FROM cases WHERE " + field + "=? ORDER BY case_id", (key,))]
            revisions: dict[int, int] = {}
            for case in cases:
                revision = case["route_revision"] if kind == "route" else policy["revision"]
                revisions[revision] = revisions.get(revision, 0) + 1
            return {"kind": kind, "key": key, "policy": policy,
                    "case_count": len(cases), "open_count": sum(x["status"] != "closed" for x in cases),
                    "revisions": [{"revision": rev, "count": count}
                                  for rev, count in sorted(revisions.items())],
                    "case_ids": [x["case_id"] for x in cases],
                    "watermark": self.store.watermark(db)}

    def timeline(self, *, kind: str, key: str) -> dict:
        if kind not in {"grid", "route"}:
            raise DomainError("BAD_POLICY", "Unknown policy kind")
        with self.store.transaction() as db:
            changes = self.store.policy_history(db, kind, key)
            if not changes:
                raise DomainError("POLICY_NOT_FOUND", "Policy does not exist", 404)
            journal = {row["sequence"]: dict(row) for row in db.execute(
                "SELECT sequence,occurred_at,operation,payload_digest FROM journal "
                "WHERE entity_id=? ORDER BY sequence", (key,))}
            items = [{**change, "journal": journal.get(change["sequence"])} for change in changes]
            return {"kind": kind, "key": key, "history": items,
                    "current_revision": items[-1]["revision"]}
