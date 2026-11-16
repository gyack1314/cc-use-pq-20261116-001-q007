"""按请求参数与日志水位固定不可变指标快照。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .contracts import DomainError, ReportInput, utc
from .metrics import MetricsService
from .store import Store, canonical, digest

class ReportService:
    def __init__(self, store: Store):
        self.store = store
        self.metrics = MetricsService(store)

    @staticmethod
    def _parameters(request: ReportInput) -> dict:
        return request.model_dump(mode="json", exclude={"report_key"})

    @staticmethod
    def _unpack(record: dict, replayed: bool = False) -> dict:
        return {
            "report_key": record["report_key"], "as_of": record["as_of"],
            "watermark": record["watermark"], "created_at": record["created_at"],
            "group_by": record["group_by"], "district": record["district"],
            "category": record["category"],
            "grouping_basis": json.loads(record["grouping_json"]),
            "body": json.loads(record["body_json"]), "replayed": replayed,
        }

    @classmethod
    def _basis(cls, db, *, watermark: int, as_of: str,
               body: dict) -> dict:
        """Freeze the grouping used by this report, not just its numbers.

        Old reports keep the membership intervals that existed at their
        watermark, so a later merge/split/undo cannot silently re-interpret
        them.  Hints still pending at freeze time are recorded as such and
        were never counted.
        """
        stamp = utc(datetime.fromisoformat(as_of))
        memberships = {}
        for item in Store.memberships(db, watermark, stamp):
            memberships.setdefault(int(item["problem_id"]), []).append(
                int(item["case_id"]))
        merges = Store.merge_state(db, watermark, stamp)
        suggestions = [dict(item) for item in db.execute(
            "SELECT suggestion_id,case_low_id,case_high_id,status,reason,created_sequence "
            "FROM association_suggestions WHERE created_sequence<=? "
            "ORDER BY suggestion_id", (watermark,))]
        return {
            "watermark": watermark, "as_of": stamp,
            "problems": {str(key): sorted(value) for key, value in sorted(memberships.items())},
            "merged_problem_ids": {str(key): value for key, value in sorted(merges.items())},
            "associations": [{"suggestion_id": item["suggestion_id"],
                              "case_ids": [item["case_low_id"], item["case_high_id"]],
                              "status": item["status"], "reason": item["reason"]}
                             for item in suggestions],
            "counted_problem_ids": sorted(
                int(group["key"].split(":", 1)[1])
                for group in body.get("groups", [])
                if str(group["key"]).startswith("problem:"))
            if body.get("group_by") == "problem" else None,
        }

    def create(self, request: ReportInput) -> dict:
        parameters = self._parameters(request)
        fingerprint = digest(parameters)
        with self.store.transaction() as db:
            old = self.store.report(db, request.report_key)
            if old is not None:
                if old["request_digest"] != fingerprint:
                    raise DomainError("REPORT_KEY_CONFLICT", "Report key has different parameters", 409)
                return self._unpack(old, replayed=True)
            watermark = self.store.watermark(db)
            body = self.metrics.calculate(db, as_of=request.as_of, group_by=request.group_by,
                                          district=request.district, category=request.category,
                                          watermark=watermark)
            basis = self._basis(db, watermark=watermark, as_of=utc(request.as_of), body=body)
            now = utc(datetime.now(timezone.utc))
            db.execute("INSERT INTO reports(report_key,request_digest,as_of,watermark,group_by,"
                       "district,category,body_json,grouping_json,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (request.report_key, fingerprint, utc(request.as_of), watermark,
                        request.group_by, request.district, request.category,
                        canonical(body), canonical(basis), now))
            return self._unpack(self.store.require_report(db, request.report_key))

    def get(self, report_key: str) -> dict:
        with self.store.connection() as db:
            return self._unpack(self.store.require_report(db, report_key))

    def compare(self, first_key: str, second_key: str) -> dict:
        with self.store.connection() as db:
            first = self.store.require_report(db, first_key)
            second = self.store.require_report(db, second_key)
            if (first["group_by"] != second["group_by"]
                    or first["district"] != second["district"]
                    or first["category"] != second["category"]):
                raise DomainError("REPORT_SCOPE_MISMATCH", "Reports must use identical grouping and filters", 409)
            a = json.loads(first["body_json"])
            b = json.loads(second["body_json"])
            keys = ("accepted", "open", "responded", "response_overdue", "resolution_overdue",
                    "closed", "solved", "explained_close", "acknowledged", "reopened",
                    "unfulfilled_response", "unfulfilled_resolution")
            deltas = {key: b["totals"].get(key, 0) - a["totals"].get(key, 0) for key in keys}
            ga = {group["key"]: group for group in a["groups"]}
            gb = {group["key"]: group for group in b["groups"]}
            groups = [{"key": key, "delta": {metric: gb.get(key, {}).get(metric, 0)
                         - ga.get(key, {}).get(metric, 0) for metric in keys}}
                      for key in sorted(set(ga) | set(gb))]
            pa = a.get("problems") or {}
            pb = b.get("problems") or {}
            problem_delta = {key: pb.get(key, 0) - pa.get(key, 0)
                             for key in ("raw_cases", "governance_problems",
                                         "grouped_cases", "ungrouped_cases")}
            return {"from": first_key, "to": second_key, "from_watermark": first["watermark"],
                    "to_watermark": second["watermark"], "totals_delta": deltas,
                    "problem_delta": problem_delta, "groups": groups}

    def list(self, after: str = "", limit: int = 50) -> dict:
        if not 1 <= limit <= 200:
            raise DomainError("BAD_PAGE", "Invalid report page size")
        with self.store.connection() as db:
            rows = [dict(item) for item in db.execute(
                "SELECT report_key,as_of,watermark,group_by,district,category,created_at "
                "FROM reports WHERE report_key>? ORDER BY report_key LIMIT ?", (after, limit))]
            return {"items": rows, "next_cursor": rows[-1]["report_key"] if rows else None}

    def verify(self, report_key: str) -> dict:
        with self.store.connection() as db:
            record = self.store.require_report(db, report_key)
            actual = self.metrics.calculate(db, as_of=datetime.fromisoformat(record["as_of"]),
                                            group_by=record["group_by"], district=record["district"],
                                            category=record["category"], watermark=record["watermark"])
            saved = json.loads(record["body_json"])
            return {"report_key": report_key, "matches": actual == saved,
                    "watermark": record["watermark"], "saved_digest": digest(saved),
                    "recomputed_digest": digest(actual),
                    "grouping_basis": record["grouping_json"]}

    def basis(self, report_key: str) -> dict:
        with self.store.connection() as db:
            record = self.store.require_report(db, report_key)
            return {"report_key": report_key, "as_of": record["as_of"],
                    "watermark": record["watermark"], "group_by": record["group_by"],
                    "grouping_basis": json.loads(record["grouping_json"])}
