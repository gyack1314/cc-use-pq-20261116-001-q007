"""按业务时点重建案件并计算回应指标。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable

from .contracts import DomainError, parse_time, utc
from .store import Store

def reconstruct(base: dict, events: Iterable[dict], as_of: str) -> dict:
    item = dict(base)
    item["status"] = "accepted"
    item["department"] = base["initial_department"]
    item["resolution"] = None
    item["first_response_at"] = None
    item["version"] = 1
    item["acknowledged"] = False
    item["explanation_count"] = 0
    item["response_count"] = 0
    item["reopen_count"] = 0
    item["last_event_at"] = base["accepted_at"]
    for event in events:
        if event["occurred_at"] > as_of:
            continue
        kind = event["kind"]
        item["last_event_at"] = event["occurred_at"]
        item["version"] = event["case_version"]
        if kind == "assigned":
            item["department"] = event["department"]
            item["status"] = "assigned"
        elif kind in {"responded", "explained"}:
            item["status"] = kind
            item["response_count"] += 1
            if kind == "explained":
                item["explanation_count"] += 1
            if item["first_response_at"] is None:
                item["first_response_at"] = event["occurred_at"]
        elif kind == "acknowledged":
            item["status"] = "acknowledged"
            item["acknowledged"] = True
        elif kind == "closed":
            item["status"] = "closed"
            item["resolution"] = event["resolution"]
        elif kind == "reopened":
            item["status"] = "reopened"
            item["resolution"] = None
            item["reopen_count"] += 1
        else:
            raise DomainError("CORRUPT_EVENT", "Unknown stored event kind", 500)
    return item

def derived(case: dict, as_of: str) -> dict:
    response_due = case["response_due"]
    resolution_due = case["resolution_due"]
    response_at = case["first_response_at"]
    responded = response_at is not None
    closed = case["status"] == "closed"
    return {
        "response_on_time": responded and response_at <= response_due,
        "response_overdue": (not responded and as_of > response_due) or
                            (responded and response_at > response_due),
        "resolution_overdue": (not closed and as_of > resolution_due) or
                              (closed and case["last_event_at"] > resolution_due),
        "responded": responded,
        "closed": closed,
        "solved": closed and case["resolution"] == "solved",
        "explained_close": closed and case["resolution"] == "explained",
        "out_of_scope": closed and case["resolution"] == "out_of_scope",
        "acknowledged": case["acknowledged"],
        "reopened": case["reopen_count"] > 0,
        "explained": case["explanation_count"] > 0,
    }

COUNTERS = (
    "accepted", "open", "responded", "response_on_time", "response_overdue",
    "resolution_overdue", "closed", "solved", "explained_close", "out_of_scope",
    "acknowledged", "reopened", "explained", "priority_one",
    "grouped", "ungrouped", "unfulfilled_response", "unfulfilled_resolution",
)

def empty_counters() -> dict[str, int]:
    return {key: 0 for key in COUNTERS}

def accrue(counters: dict, case: dict, flags: dict) -> None:
    counters["accepted"] += 1
    counters["open"] += int(not flags["closed"])
    for field in ("responded", "response_on_time", "response_overdue",
                  "resolution_overdue", "closed", "solved", "explained_close",
                  "out_of_scope", "acknowledged", "reopened", "explained"):
        counters[field] += int(flags[field])
    counters["priority_one"] += int(case["priority"] == 1)
    grouped = case.get("problem_id") is not None
    counters["grouped"] += int(grouped)
    counters["ungrouped"] += int(not grouped)
    # Resident obligations are tracked per case, never per governance problem.
    counters["unfulfilled_response"] += int(not flags["responded"])
    counters["unfulfilled_resolution"] += int(not flags["closed"])

def ratios(counters: dict) -> dict:
    accepted = counters["accepted"]
    responded = counters["responded"]
    closed = counters["closed"]
    return {
        "response_rate": round(responded / accepted, 4) if accepted else None,
        "on_time_response_rate": round(counters["response_on_time"] / responded, 4)
                                 if responded else None,
        "closure_rate": round(closed / accepted, 4) if accepted else None,
        "solution_share_of_closed": round(counters["solved"] / closed, 4) if closed else None,
        "acknowledgment_rate": round(counters["acknowledged"] / responded, 4)
                               if responded else None,
    }

def problem_summary(raw_cases: int, grouped: int, problems: set[int]) -> dict:
    """Keep the three quantities visibly separate.

    ``raw_cases`` counts every independently sourced request, ``problems``
    counts confirmed governance problems, and unfulfilled obligations are
    counted per resident request.  Similar-content hints do not appear here
    until someone confirms them.
    """
    return {
        "raw_cases": raw_cases,
        "governance_problems": len(problems),
        "grouped_cases": grouped,
        "ungrouped_cases": raw_cases - grouped,
        "cases_per_problem": round(grouped / len(problems), 4) if problems else None,
    }

class MetricsService:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _group(case: dict, key: str) -> str:
        if key == "problem":
            return ("problem:" + str(case["problem_id"])
                    if case.get("problem_id") is not None else "ungrouped")
        return str(case[{"district": "district", "grid": "grid_id", "category": "category",
                         "department": "department"}[key]])

    def calculate(self, db, *, as_of: datetime, group_by: str,
                  district: str | None = None, category: str | None = None,
                  watermark: int | None = None, include_cases: bool = False,
                  include_problems: bool = True) -> dict:
        stamp = utc(as_of)
        if group_by not in {"district", "grid", "category", "department", "problem"}:
            raise DomainError("BAD_GROUP", "Unsupported grouping")
        if watermark is None:
            watermark = self.store.watermark(db)
        filters = ["created_sequence<=?", "accepted_at<=?"]
        params: list[object] = [watermark, stamp]
        if district is not None:
            filters.append("district=?")
            params.append(district)
        if category is not None:
            filters.append("category=?")
            params.append(category)
        query = "SELECT * FROM cases WHERE " + " AND ".join(filters) + " ORDER BY case_id"
        rows = [dict(value) for value in db.execute(query, params)]
        # Grouping is resolved independently per watermark and business time,
        # so an old report keeps the grouping basis it was frozen with.
        membership = Store.membership_state(db, watermark, stamp)
        merge_state = Store.merge_state(db, watermark, stamp)
        totals = empty_counters()
        grouped: dict[str, dict] = defaultdict(empty_counters)
        details = []
        problems: set[int] = set()
        for base in rows:
            events = self.store.events(db, base["case_id"], watermark)
            case = reconstruct(base, events, stamp)
            member = membership.get(base["case_id"])
            problem_id = member["problem_id"] if member else None
            if problem_id is not None:
                problem_id = merge_state.get(problem_id, problem_id)
            case["problem_id"] = problem_id
            flags = derived(case, stamp)
            accrue(totals, case, flags)
            accrue(grouped[self._group(case, group_by)], case, flags)
            if problem_id is not None:
                problems.add(problem_id)
            if include_cases:
                details.append({"case_id": case["case_id"], "status": case["status"],
                                "version": case["version"], "department": case["department"],
                                "district": case["district"], "problem_id": problem_id,
                                "flags": flags})
        result = {
            "as_of": stamp, "watermark": watermark, "group_by": group_by,
            "filters": {"district": district, "category": category},
            "totals": {**totals, **ratios(totals)},
            "groups": [{"key": key, **value, **ratios(value)} for key, value in sorted(grouped.items())],
            **({"cases": details} if include_cases else {}),
        }
        if include_problems:
            result["problems"] = problem_summary(totals["accepted"], totals["grouped"],
                                                 problems)
        return result

    def preview(self, *, as_of: datetime, group_by: str = "district",
                district: str | None = None, category: str | None = None,
                include_cases: bool = False, watermark: int | None = None) -> dict:
        with self.store.transaction() as db:
            current = self.store.watermark(db)
            if watermark is not None:
                if watermark < 0 or watermark > current:
                    raise DomainError("BAD_WATERMARK", "Watermark is outside the journal",
                                      422, {"watermark": watermark, "current": current})
            return self.calculate(db, as_of=as_of, group_by=group_by,
                                  district=district, category=category,
                                  include_cases=include_cases, watermark=watermark)

    def case_at(self, case_id: int, as_of: datetime) -> dict:
        with self.store.transaction() as db:
            base = self.store.case(db, case_id)
            stamp = utc(as_of)
            if base["accepted_at"] > stamp:
                raise DomainError("CASE_NOT_YET_ACCEPTED", "Case was not accepted at time", 404)
            watermark = self.store.watermark(db)
            current = reconstruct(base, self.store.events(db, case_id, watermark), stamp)
            member = Store.membership_state(db, watermark, stamp).get(case_id)
            problem_id = member["problem_id"] if member else None
            if problem_id is not None:
                problem_id = Store.merge_state(db, watermark, stamp).get(problem_id, problem_id)
            current["problem_id"] = problem_id
            current["indicators"] = derived(current, stamp)
            current["watermark"] = watermark
            return current
