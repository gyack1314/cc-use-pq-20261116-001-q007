"""对已提交数据进行质量核对和业务对账。"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from .contracts import DomainError, utc
from .metrics import MetricsService
from .store import Store

class QualityService:
    def __init__(self, store: Store):
        self.store = store
        self.metrics = MetricsService(store)

    @staticmethod
    def _issue(code: str, entity: str, identifier: str, detail: str) -> dict:
        return {"code": code, "entity": entity, "id": identifier, "detail": detail}

    def inspect(self, *, as_of: datetime, limit: int = 200) -> dict:
        if not 1 <= limit <= 1000:
            raise DomainError("BAD_LIMIT", "Invalid issue limit")
        issues: list[dict] = []
        stamp = utc(as_of)
        with self.store.transaction() as db:
            watermark = self.store.watermark(db)
            batches = [dict(row) for row in db.execute("SELECT * FROM batches ORDER BY batch_key")]
            cases = [dict(row) for row in db.execute("SELECT * FROM cases ORDER BY case_id")]
            events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY event_id")]
            reports = [dict(row) for row in db.execute("SELECT * FROM reports ORDER BY report_key")]
            for case in cases:
                key = str(case["case_id"])
                if case["accepted_at"] > case["last_event_at"]:
                    issues.append(self._issue("TIME_REVERSED", "case", key,
                                              "Last event predates acceptance"))
                if case["response_due"] > case["resolution_due"]:
                    issues.append(self._issue("DEADLINE_REVERSED", "case", key,
                                              "Response deadline follows resolution"))
                if case["first_response_at"] and case["first_response_at"] > case["last_event_at"]:
                    issues.append(self._issue("RESPONSE_TIME_REVERSED", "case", key,
                                              "First response follows last event"))
                if case["status"] == "closed" and not case["resolution"]:
                    issues.append(self._issue("CLOSED_WITHOUT_RESOLUTION", "case", key,
                                              "Closed case needs resolution"))
                if case["status"] != "closed" and case["resolution"]:
                    issues.append(self._issue("OPEN_WITH_RESOLUTION", "case", key,
                                              "Open case retains closure value"))
                case_events = [row for row in events if row["case_id"] == case["case_id"]]
                if len(case_events) + 1 != case["version"]:
                    issues.append(self._issue("VERSION_GAP", "case", key,
                                              "Event count differs from current version"))
                expected_versions = list(range(2, case["version"] + 1))
                actual_versions = sorted(row["case_version"] for row in case_events)
                if actual_versions != expected_versions:
                    issues.append(self._issue("EVENT_VERSION_GAP", "case", key,
                                              "Event sequence is not contiguous"))
                if not db.execute("SELECT 1 FROM journal WHERE sequence=?",
                                  (case["created_sequence"],)).fetchone():
                    issues.append(self._issue("MISSING_JOURNAL", "case", key,
                                              "Acceptance has no journal entry"))
            for event in events:
                if not db.execute("SELECT 1 FROM journal WHERE sequence=?",
                                  (event["event_sequence"],)).fetchone():
                    issues.append(self._issue("MISSING_JOURNAL", "event", str(event["event_id"]),
                                              "Event has no journal entry"))
            for batch in batches:
                count = sum(batch["sequence_start"] <= case["created_sequence"] <=
                            batch["sequence_end"] for case in cases)
                if count != batch["imported_count"]:
                    issues.append(self._issue("BATCH_COUNT_MISMATCH", "batch", batch["batch_key"],
                                              "Batch sequence interval does not match case count"))
            for report in reports:
                if report["watermark"] > watermark:
                    issues.append(self._issue("FUTURE_WATERMARK", "report", report["report_key"],
                                              "Report references uncommitted journal sequence"))
            totals = self.metrics.calculate(db, as_of=as_of, group_by="district", watermark=watermark)
            return {"as_of": stamp, "watermark": watermark, "counts": {
                "batches": len(batches), "cases": len(cases), "events": len(events),
                "reports": len(reports)}, "issue_count": len(issues),
                "issues": issues[:limit], "truncated": len(issues) > limit,
                "metrics_acceptance_count": totals["totals"]["accepted"]}

    def completeness(self, *, as_of: datetime, district: str | None = None) -> dict:
        stamp = utc(as_of)
        with self.store.transaction() as db:
            result = self.metrics.calculate(db, as_of=as_of, group_by="category",
                                            district=district, include_cases=True)
            categories = [dict(row) for row in db.execute(
                "SELECT category,department,response_hours,resolution_hours FROM route_rules "
                "ORDER BY category")]
            counts = {row["key"]: row["accepted"] for row in result["groups"]}
            return {"as_of": stamp, "district": district, "configured_categories": categories,
                    "case_counts": counts, "unseen_categories": [row["category"] for row in categories
                                                            if row["category"] not in counts],
                    "totals": result["totals"], "watermark": result["watermark"]}

    def responsibility_load(self, *, as_of: datetime) -> dict:
        with self.store.transaction() as db:
            result = self.metrics.calculate(db, as_of=as_of, group_by="department",
                                            include_cases=True)
            catalog = {row["department"] for row in db.execute(
                "SELECT DISTINCT department FROM route_rules")}
            groups = {row["key"]: row for row in result["groups"]}
            return {"as_of": result["as_of"], "watermark": result["watermark"],
                    "departments": [{"department": key, "configured": key in catalog,
                                     "accepted": groups.get(key, {}).get("accepted", 0),
                                     "open": groups.get(key, {}).get("open", 0),
                                     "response_overdue": groups.get(key, {}).get("response_overdue", 0),
                                     "resolution_overdue": groups.get(key, {}).get("resolution_overdue", 0)}
                                    for key in sorted(catalog | set(groups))]}

    def coverage(self, *, as_of: datetime) -> dict:
        with self.store.transaction() as db:
            result = self.metrics.calculate(db, as_of=as_of, group_by="grid")
            groups = {item["key"]: item for item in result["groups"]}
            rows = []
            for grid in db.execute("SELECT grid_id,district,active FROM grids ORDER BY grid_id"):
                count = groups.get(grid["grid_id"], {}).get("accepted", 0)
                rows.append({"grid_id": grid["grid_id"], "district": grid["district"],
                             "active": bool(grid["active"]), "accepted": count,
                             "has_observations": count > 0})
            return {"as_of": result["as_of"], "watermark": result["watermark"],
                    "grids": rows, "grids_with_cases": sum(x["has_observations"] for x in rows),
                    "grids_without_cases": sum(not x["has_observations"] for x in rows)}
