"""批次原子导入、幂等处理及受理时路由快照。"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from .contracts import BatchInput, DomainError, utc
from .routing import RoutingService
from .store import Store, digest

class IntakeService:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _case_identity(case) -> tuple[str, str]:
        return case.source, case.external_id

    @staticmethod
    def _resident_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _case_payload(case) -> dict:
        value = case.model_dump(mode="json")
        value.pop("resident_token")
        value["resident_hash"] = IntakeService._resident_hash(case.resident_token)
        return value

    def import_batch(self, batch: BatchInput) -> dict:
        submitted = utc(batch.submitted_at)
        payload = {"submitted_at": submitted, "cases": [self._case_payload(x) for x in batch.cases]}
        fingerprint = digest(payload)
        identities = [self._case_identity(x) for x in batch.cases]
        if len(identities) != len(set(identities)):
            raise DomainError("BATCH_DUPLICATE", "Batch repeats a source identity", 409)
        if any(utc(x.occurred_at) > submitted for x in batch.cases):
            raise DomainError("FUTURE_CASE", "Case occurred after batch submission", 422)
        with self.store.transaction() as db:
            prior = self.store.batch(db, batch.batch_key)
            if prior is not None:
                if prior["digest"] != fingerprint:
                    raise DomainError("BATCH_KEY_CONFLICT", "Batch key has different content", 409)
                return {**prior, "replayed": True}
            for case in batch.cases:
                old = self.store.case_by_external(db, case.source, case.external_id)
                if old is not None:
                    raise DomainError("CASE_ID_CONFLICT", "Source identity already imported", 409,
                                      {"source": case.source, "external_id": case.external_id})
                route = RoutingService.resolve(db, case.grid_id, case.category, case.occurred_at)
                event_payload = self._case_payload(case)
                sequence = self.store.journal(db, "case_accepted", "case",
                                              case.source + ":" + case.external_id,
                                              event_payload, utc(case.occurred_at))
                db.execute("INSERT INTO cases(source,external_id,grid_id,district,category,summary,"
                           "resident_hash,priority,accepted_at,department,initial_department,route_revision,response_due,"
                           "resolution_due,status,resolution,first_response_at,last_event_at,version,"
                           "created_sequence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (case.source, case.external_id, route.grid_id, route.district,
                            route.category, case.summary, self._resident_hash(case.resident_token),
                            case.priority, utc(case.occurred_at), route.department, route.department,
                            route.route_revision, route.response_due, route.resolution_due,
                            "accepted", None, None, utc(case.occurred_at), 1, sequence))
            first = db.execute("SELECT MIN(created_sequence) FROM cases WHERE "
                               "created_sequence > (SELECT COALESCE(MAX(sequence_end),0) FROM batches)").fetchone()[0]
            last = self.store.watermark(db)
            db.execute("INSERT INTO batches VALUES(?,?,?,?,?,?)",
                       (batch.batch_key, fingerprint, submitted, len(batch.cases), first, last))
            return {**self.store.batch(db, batch.batch_key), "replayed": False}

    def get_batch(self, batch_key: str) -> dict:
        with self.store.connection() as db:
            result = self.store.batch(db, batch_key)
            if result is None:
                raise DomainError("BATCH_NOT_FOUND", "Batch does not exist", 404)
            return result

    def import_summary(self) -> dict:
        with self.store.connection() as db:
            counts = self.store.table_counts(db)
            return {"batches": counts["batches"], "cases": counts["cases"],
                    "watermark": self.store.watermark(db)}

    def batch_trace(self, batch_key: str) -> dict:
        with self.store.connection() as db:
            batch = self.store.batch(db, batch_key)
            if batch is None:
                raise DomainError("BATCH_NOT_FOUND", "Batch does not exist", 404)
            return {"batch": batch, "cases": self.store.batch_cases(db, batch_key),
                    "journal": self.store.journal_span(db, batch["sequence_start"],
                                                       batch["sequence_end"])}
