"""根据已提交案件状态生成工作队列。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .contracts import DomainError, utc
from .metrics import MetricsService
from .store import Store

class OperationsService:
    def __init__(self, store: Store):
        self.store = store
        self.metrics = MetricsService(store)

    def queue(self, *, as_of: datetime, department: str | None = None,
              district: str | None = None, include_closed: bool = False,
              after: int = 0, limit: int = 50) -> dict:
        if after < 0 or not 1 <= limit <= 200:
            raise DomainError("BAD_PAGE", "Invalid queue cursor or limit")
        with self.store.transaction() as db:
            snapshot = self.metrics.calculate(db, as_of=as_of, group_by="department",
                                              district=district, include_cases=True)
            timestamp = snapshot["as_of"]
            details = []
            for item in snapshot["cases"]:
                if item["case_id"] <= after:
                    continue
                if department and item["department"] != department:
                    continue
                if not include_closed and item["flags"]["closed"]:
                    continue
                base = self.store.case(db, item["case_id"])
                details.append({"case_id": item["case_id"], "external_id": base["external_id"],
                                "grid_id": base["grid_id"], "district": base["district"],
                                "category": base["category"], "department": item["department"],
                                "priority": base["priority"], "status": item["status"],
                                "problem_id": item.get("problem_id"),
                                "accepted_at": base["accepted_at"],
                                "response_due": base["response_due"],
                                "resolution_due": base["resolution_due"],
                                "response_overdue": item["flags"]["response_overdue"],
                                "resolution_overdue": item["flags"]["resolution_overdue"]})
            selected = details[:limit]
            return {"as_of": timestamp, "watermark": snapshot["watermark"],
                    "items": selected, "next_cursor": selected[-1]["case_id"] if selected else None,
                    "has_more": len(details) > limit}

    def pressure(self, *, as_of: datetime, district: str | None = None) -> dict:
        with self.store.transaction() as db:
            report = self.metrics.calculate(db, as_of=as_of, group_by="department",
                                            district=district)
            groups = []
            for item in report["groups"]:
                unresolved = item["open"]
                overdue = item["response_overdue"] + item["resolution_overdue"]
                groups.append({"department": item["key"], "open": unresolved,
                               "overdue_signals": overdue,
                               "pressure_index": round(overdue / unresolved, 4) if unresolved else None,
                               "acknowledged": item["acknowledged"]})
            groups.sort(key=lambda row: (-row["overdue_signals"], row["department"]))
            return {"as_of": report["as_of"], "watermark": report["watermark"],
                    "district": district, "groups": groups,
                    "total_open": report["totals"]["open"]}

    def aging(self, *, as_of: datetime, district: str | None = None) -> dict:
        with self.store.transaction() as db:
            report = self.metrics.calculate(db, as_of=as_of, group_by="district",
                                            district=district, include_cases=True)
            buckets = {"under_24h": 0, "one_to_three_days": 0,
                       "three_to_seven_days": 0, "over_seven_days": 0}
            now = as_of.astimezone()
            for item in report["cases"]:
                if item["flags"]["closed"]:
                    continue
                base = self.store.case(db, item["case_id"])
                age = (now - datetime.fromisoformat(base["accepted_at"])).total_seconds() / 3600
                if age < 24:
                    bucket = "under_24h"
                elif age < 72:
                    bucket = "one_to_three_days"
                elif age < 168:
                    bucket = "three_to_seven_days"
                else:
                    bucket = "over_seven_days"
                buckets[bucket] += 1
            return {"as_of": report["as_of"], "watermark": report["watermark"],
                    "district": district, "buckets": buckets,
                    "open_total": report["totals"]["open"]}
