"""解释响应、结案和路由差异的分析视图。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .contracts import DomainError
from .metrics import MetricsService
from .store import Store

class DiagnosticsService:
    def __init__(self, store: Store):
        self.store = store
        self.metrics = MetricsService(store)

    def response_gap(self, *, as_of: datetime, group_by: str = "district",
                     minimum_cases: int = 1) -> dict:
        if not 1 <= minimum_cases <= 10000:
            raise DomainError("BAD_THRESHOLD", "Invalid minimum case threshold")
        with self.store.transaction() as db:
            snapshot = self.metrics.calculate(db, as_of=as_of, group_by=group_by)
            eligible = [item for item in snapshot["groups"]
                        if item["accepted"] >= minimum_cases]
            groups = []
            for item in eligible:
                responded = item["responded"]
                unresolved = item["accepted"] - item["closed"]
                groups.append({"key": item["key"], "accepted": item["accepted"],
                               "responded": responded, "closed": item["closed"],
                               "awaiting_first_response": item["accepted"] - responded,
                               "open_after_response": max(0, responded - item["closed"]),
                               "response_overdue": item["response_overdue"],
                               "resolution_overdue": item["resolution_overdue"],
                               "unresolved_share": round(unresolved / item["accepted"], 4),
                               "response_rate": item["response_rate"]})
            groups.sort(key=lambda item: (-item["awaiting_first_response"], item["key"]))
            return {"as_of": snapshot["as_of"], "watermark": snapshot["watermark"],
                    "group_by": group_by, "minimum_cases": minimum_cases,
                    "excluded_small_groups": len(snapshot["groups"]) - len(groups),
                    "groups": groups}

    def route_exposure(self, *, as_of: datetime) -> dict:
        with self.store.transaction() as db:
            snapshot = self.metrics.calculate(db, as_of=as_of, group_by="category",
                                              include_cases=True)
            current = {row["category"]: dict(row) for row in db.execute(
                "SELECT * FROM route_rules")}
            divergence: dict[str, dict] = defaultdict(lambda: {"count": 0, "open": 0,
                                                                "closed": 0, "old_revisions": set()})
            for case in snapshot["cases"]:
                base = self.store.case(db, case["case_id"])
                rule = current.get(base["category"])
                if rule is None or base["route_revision"] != rule["revision"]:
                    key = base["category"]
                    slot = divergence[key]
                    slot["count"] += 1
                    slot["open"] += int(not case["flags"]["closed"])
                    slot["closed"] += int(case["flags"]["closed"])
                    slot["old_revisions"].add(base["route_revision"])
            rows = [{"category": key, "cases_on_prior_revision": item["count"],
                     "open": item["open"], "closed": item["closed"],
                     "prior_revisions": sorted(item["old_revisions"]),
                     "current_revision": current.get(key, {}).get("revision")}
                    for key, item in sorted(divergence.items())]
            return {"as_of": snapshot["as_of"], "watermark": snapshot["watermark"],
                    "categories": rows, "affected_cases": sum(x["cases_on_prior_revision"] for x in rows)}

    def channel_mix(self, *, as_of: datetime, district: str | None = None) -> dict:
        with self.store.transaction() as db:
            snapshot = self.metrics.calculate(db, as_of=as_of, group_by="district",
                                              district=district, include_cases=True)
            channels: dict[str, dict] = defaultdict(lambda: {"accepted": 0, "responded": 0,
                                                              "closed": 0, "response_overdue": 0})
            for item in snapshot["cases"]:
                base = self.store.case(db, item["case_id"])
                target = channels[base["source"]]
                target["accepted"] += 1
                target["responded"] += int(item["flags"]["responded"])
                target["closed"] += int(item["flags"]["closed"])
                target["response_overdue"] += int(item["flags"]["response_overdue"])
            result = []
            for key, counts in sorted(channels.items()):
                result.append({"source": key, **counts,
                               "response_rate": round(counts["responded"] / counts["accepted"], 4)})
            return {"as_of": snapshot["as_of"], "watermark": snapshot["watermark"],
                    "district": district, "channels": result}
