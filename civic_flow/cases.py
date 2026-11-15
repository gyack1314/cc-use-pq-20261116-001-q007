"""诉求状态流转、版本控制与不可变回应历史。"""
from __future__ import annotations

from datetime import datetime

from .contracts import DomainError, EventInput, EventKind, utc
from .store import Store, digest

class CaseService:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _payload(event: EventInput) -> dict:
        return event.model_dump(mode="json", exclude={"expected_version"})

    @staticmethod
    def _transition(case: dict, event: EventInput) -> dict:
        status = case["status"]
        kind = event.kind
        if utc(event.occurred_at) < case["last_event_at"]:
            raise DomainError("EVENT_TIME_ORDER", "Event precedes case history", 409)
        if status == "closed" and kind != EventKind.REOPEN:
            raise DomainError("CASE_CLOSED", "Closed case only accepts reopening", 409)
        if status != "closed" and kind == EventKind.REOPEN:
            raise DomainError("CASE_OPEN", "Only closed case can reopen", 409)
        if kind == EventKind.ASSIGN:
            if status not in {"accepted", "assigned", "responded", "explained", "reopened"}:
                raise DomainError("BAD_TRANSITION", "Assignment is not allowed", 409)
            return {"department": event.department, "status": "assigned"}
        if kind in {EventKind.RESPONSE, EventKind.EXPLAIN}:
            if status not in {"accepted", "assigned", "responded", "explained", "reopened"}:
                raise DomainError("BAD_TRANSITION", "Response is not allowed", 409)
            changes = {"status": "explained" if kind == EventKind.EXPLAIN else "responded"}
            if case["first_response_at"] is None:
                changes["first_response_at"] = utc(event.occurred_at)
            return changes
        if kind == EventKind.ACK:
            if status not in {"responded", "explained"}:
                raise DomainError("ACK_BEFORE_RESPONSE", "Acknowledgment needs response", 409)
            return {"status": "acknowledged"}
        if kind == EventKind.CLOSE:
            if status not in {"responded", "explained", "acknowledged"}:
                raise DomainError("CLOSE_BEFORE_RESPONSE", "Closure needs response", 409)
            if event.resolution.value == "solved" and status == "explained":
                raise DomainError("SOLUTION_MISMATCH", "Explanation alone is not a solution", 409)
            return {"status": "closed", "resolution": event.resolution.value}
        if kind == EventKind.REOPEN:
            return {"status": "reopened", "resolution": None}
        raise DomainError("BAD_EVENT", "Unknown case event")

    def append(self, case_id: int, event: EventInput) -> dict:
        payload = self._payload(event)
        fingerprint = digest(payload)
        with self.store.transaction() as db:
            case = self.store.case(db, case_id)
            prior = self.store.event_by_key(db, case_id, event.event_key)
            if prior is not None:
                if prior["digest"] != fingerprint:
                    raise DomainError("EVENT_KEY_CONFLICT", "Event key has different content", 409)
                return {"case": case, "event": prior, "replayed": True}
            if case["version"] != event.expected_version:
                raise DomainError("VERSION_CONFLICT", "Case version changed", 409,
                                  {"expected": event.expected_version, "actual": case["version"]})
            changes = self._transition(case, event)
            version = case["version"] + 1
            sequence = self.store.journal(db, "case_event", "case", str(case_id), payload,
                                          utc(event.occurred_at))
            db.execute("INSERT INTO events(case_id,event_key,kind,actor,occurred_at,department,"
                       "note,resolution,case_version,event_sequence,digest) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (case_id, event.event_key, event.kind.value, event.actor,
                        utc(event.occurred_at), event.department, event.note,
                        event.resolution.value if event.resolution else None,
                        version, sequence, fingerprint))
            changes["version"] = version
            changes["last_event_at"] = utc(event.occurred_at)
            fields = list(changes)
            db.execute("UPDATE cases SET " + ",".join(key + "=?" for key in fields) + " WHERE case_id=?",
                       [changes[key] for key in fields] + [case_id])
            current = self.store.case(db, case_id)
            inserted = self.store.event_by_key(db, case_id, event.event_key)
            return {"case": current, "event": inserted, "replayed": False}

    def get(self, case_id: int, *, history: bool = False) -> dict:
        with self.store.connection() as db:
            case = self.store.case(db, case_id)
            if history:
                case["events"] = self.store.events(db, case_id)
            return case

    def list(self, district: str | None = None, category: str | None = None,
             status: str | None = None, after: int = 0, limit: int = 50) -> dict:
        with self.store.connection() as db:
            items = self.store.case_list(db, district=district, category=category,
                                         status=status, after=after, limit=limit)
            return {"items": items, "next_cursor": items[-1]["case_id"] if items else None,
                    "watermark": self.store.watermark(db)}

    def history(self, case_id: int) -> list[dict]:
        with self.store.connection() as db:
            self.store.case(db, case_id)
            return self.store.events(db, case_id)

    def audit(self, after: int = 0, limit: int = 100) -> dict:
        with self.store.connection() as db:
            items = self.store.changes(db, after, limit)
            return {"items": items, "next_cursor": items[-1]["sequence"] if items else None,
                    "watermark": self.store.watermark(db)}

    def timeline(self, case_id: int) -> dict:
        with self.store.connection() as db:
            case = self.store.case(db, case_id)
            events = self.store.events(db, case_id)
            return {"case_id": case_id, "accepted_at": case["accepted_at"],
                    "initial_department": case["initial_department"],
                    "current_department": case["department"], "current_status": case["status"],
                    "timeline": [{"version": 1, "kind": "accepted", "at": case["accepted_at"],
                                  "department": case["initial_department"],
                                  "sequence": case["created_sequence"]}]
                    + [{"version": item["case_version"], "kind": item["kind"],
                        "at": item["occurred_at"], "department": item["department"],
                        "sequence": item["event_sequence"]} for item in events]}
