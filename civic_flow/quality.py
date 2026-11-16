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
            issues.extend(self._problem_issues(db, cases, events))
            totals = self.metrics.calculate(db, as_of=as_of, group_by="district", watermark=watermark)
            problem_counts = self.metrics.calculate(db, as_of=as_of, group_by="problem",
                                                    watermark=watermark, include_problems=True)
            return {"as_of": stamp, "watermark": watermark, "counts": {
                "batches": len(batches), "cases": len(cases), "events": len(events),
                "reports": len(reports),
                "problems": int(db.execute("SELECT COUNT(*) FROM problems").fetchone()[0]),
                "active_memberships": int(db.execute(
                    "SELECT COUNT(*) FROM problem_members WHERE left_sequence IS NULL"
                ).fetchone()[0]),
                "pending_associations": int(db.execute(
                    "SELECT COUNT(*) FROM association_suggestions WHERE status='pending'"
                ).fetchone()[0])},
                "issue_count": len(issues),
                "issues": issues[:limit], "truncated": len(issues) > limit,
                "metrics_acceptance_count": totals["totals"]["accepted"],
                "problem_metrics": problem_counts["problems"]}

    def _problem_issues(self, db, cases: list[dict], events: list[dict]) -> list[dict]:
        """Detect broken grouping without ever counting hints as problems."""
        issues: list[dict] = []
        case_ids = {case["case_id"] for case in cases}
        active = [dict(item) for item in db.execute(
            "SELECT * FROM problem_members WHERE left_sequence IS NULL")]
        seen: dict[int, int] = {}
        for item in active:
            if item["case_id"] not in case_ids:
                issues.append(self._issue("ORPHAN_MEMBERSHIP", "problem",
                                          str(item["problem_id"]),
                                          "Membership references a missing case"))
                continue
            if item["case_id"] in seen:
                issues.append(self._issue("DUPLICATE_MEMBERSHIP", "case",
                                          str(item["case_id"]),
                                          "Case is in two active problems"))
            seen[item["case_id"]] = item["problem_id"]
            if not db.execute("SELECT 1 FROM journal WHERE sequence=?",
                              (item["joined_sequence"],)).fetchone():
                issues.append(self._issue("MISSING_JOURNAL", "membership",
                                          str(item["member_id"]),
                                          "Membership has no journal entry"))
            if item["left_sequence"] is not None and not db.execute(
                    "SELECT 1 FROM journal WHERE sequence=?",
                    (item["left_sequence"],)).fetchone():
                issues.append(self._issue("MISSING_JOURNAL", "membership",
                                          str(item["member_id"]),
                                          "Membership closure has no journal entry"))
        for problem in db.execute("SELECT problem_id FROM problems ORDER BY problem_id"):
            problem_id = int(problem["problem_id"])
            if not db.execute("SELECT 1 FROM problem_members WHERE problem_id=? LIMIT 1",
                              (problem_id,)).fetchone():
                issues.append(self._issue("PROBLEM_WITHOUT_CASES", "problem",
                                          str(problem_id), "Problem has no case history"))
            if not db.execute("SELECT 1 FROM journal WHERE entity='problem' AND entity_id=?",
                              (str(problem_id),)).fetchone():
                issues.append(self._issue("MISSING_JOURNAL", "problem", str(problem_id),
                                          "Problem has no journal entry"))
        for suggestion in db.execute("SELECT * FROM association_suggestions"):
            if suggestion["status"] == "pending":
                continue
            if suggestion["resolved_sequence"] is None or not db.execute(
                    "SELECT 1 FROM journal WHERE sequence=?",
                    (suggestion["resolved_sequence"],)).fetchone():
                issues.append(self._issue("MISSING_JOURNAL", "association",
                                          str(suggestion["suggestion_id"]),
                                          "Resolved suggestion has no journal entry"))
            if suggestion["status"] == "confirmed" and \
                    suggestion["confirmed_problem_id"] is None:
                issues.append(self._issue("CONFIRMED_WITHOUT_PROBLEM", "association",
                                          str(suggestion["suggestion_id"]),
                                          "Confirmed association has no problem"))
            if suggestion["status"] == "confirmed":
                # At the moment of confirmation both cases must have been
                # grouped under the problem the hint claims.  Later splits or
                # merges are legitimate and must not be reported as defects,
                # so the check uses the historical watermark.
                at = int(suggestion["resolved_sequence"])
                owner = {case_id: item["problem_id"] for case_id, item
                         in self.store.membership_state(db, at).items()}
                chain = self.store.merge_state(db, at)
                target = suggestion["confirmed_problem_id"]
                for case_id in (suggestion["case_low_id"], suggestion["case_high_id"]):
                    current = owner.get(case_id)
                    if current is not None:
                        current = chain.get(current, current)
                    if current != target:
                        issues.append(self._issue(
                            "ASSOCIATION_GROUPING_MISMATCH", "association",
                            str(suggestion["suggestion_id"]),
                            "Confirmed hint was not reflected in grouping"))
                        break
        for merge in db.execute("SELECT * FROM problem_merges ORDER BY merge_id"):
            if not db.execute("SELECT 1 FROM journal WHERE sequence=?",
                              (merge["merge_sequence"],)).fetchone():
                issues.append(self._issue("MISSING_JOURNAL", "merge",
                                          str(merge["merge_id"]),
                                          "Merge has no journal entry"))
            if merge["reversed_sequence"] is not None and not db.execute(
                    "SELECT 1 FROM journal WHERE sequence=?",
                    (merge["reversed_sequence"],)).fetchone():
                issues.append(self._issue("MISSING_JOURNAL", "merge",
                                          str(merge["merge_id"]),
                                          "Merge reversal has no journal entry"))
            survivor = db.execute("SELECT 1 FROM problems WHERE problem_id=?",
                                  (merge["survivor_problem_id"],)).fetchone()
            merged = db.execute("SELECT 1 FROM problems WHERE problem_id=?",
                                (merge["merged_problem_id"],)).fetchone()
            if not survivor or not merged:
                issues.append(self._issue("MERGE_TARGET_MISSING", "merge",
                                          str(merge["merge_id"]),
                                          "Merge references a missing problem"))
        return issues

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

    def problem_reconciliation(self, *, as_of: datetime) -> dict:
        """Show raw requests, governance problems and obligations separately."""
        stamp = utc(as_of)
        with self.store.transaction() as db:
            watermark = self.store.watermark(db)
            result = self.metrics.calculate(db, as_of=as_of, group_by="problem",
                                            watermark=watermark)
            cases = [dict(item) for item in db.execute(
                "SELECT case_id,source,status FROM cases ORDER BY case_id")]
            state = self.store.membership_state(db, watermark, stamp)
            pending = [dict(item) for item in db.execute(
                "SELECT suggestion_id,case_low_id,case_high_id,reason,similarity,"
                "created_at FROM association_suggestions WHERE status='pending' "
                "ORDER BY suggestion_id")]
            sources: dict[str, int] = {}
            for case in cases:
                sources[case["source"]] = sources.get(case["source"], 0) + 1
            known = {item["case_id"] for item in cases}
            return {
                "as_of": stamp, "watermark": watermark,
                "raw_cases": len(cases),
                "raw_cases_by_source": dict(sorted(sources.items())),
                "governance_problems": result["problems"]["governance_problems"],
                "grouped_cases": result["problems"]["grouped_cases"],
                "ungrouped_cases": result["problems"]["ungrouped_cases"],
                "problem_rows": result["groups"],
                "pending_associations": pending,
                "pending_association_count": len(pending),
                "unfulfilled_obligations": {
                    "response": result["totals"]["unfulfilled_response"],
                    "resolution": result["totals"]["unfulfilled_resolution"],
                },
                "orphan_memberships": sum(1 for case_id in state if case_id not in known),
            }

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
