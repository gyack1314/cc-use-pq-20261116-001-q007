"""在正式导入之前检查批次业务条件。"""
from __future__ import annotations

from collections import Counter

from .contracts import BatchInput, utc
from .routing import RoutingService
from .store import Store

class ScreeningService:
    def __init__(self, store: Store):
        self.store = store

    def preflight(self, batch: BatchInput) -> dict:
        issues = []
        keys = [(case.source, case.external_id) for case in batch.cases]
        repeated = {key for key, count in Counter(keys).items() if count > 1}
        with self.store.transaction() as db:
            existing_batch = self.store.batch(db, batch.batch_key)
            for index, case in enumerate(batch.cases):
                identity = (case.source, case.external_id)
                if identity in repeated:
                    issues.append({"index": index, "code": "BATCH_DUPLICATE",
                                   "identity": list(identity)})
                if self.store.case_by_external(db, *identity):
                    issues.append({"index": index, "code": "CASE_ID_CONFLICT",
                                   "identity": list(identity)})
                grid = self.store.grid(db, case.grid_id)
                if grid is None:
                    issues.append({"index": index, "code": "GRID_UNKNOWN",
                                   "grid_id": case.grid_id})
                elif not grid["active"]:
                    issues.append({"index": index, "code": "GRID_INACTIVE",
                                   "grid_id": case.grid_id})
                if self.store.rule(db, case.category) is None:
                    issues.append({"index": index, "code": "ROUTE_UNKNOWN",
                                   "category": case.category})
                if utc(case.occurred_at) > utc(batch.submitted_at):
                    issues.append({"index": index, "code": "FUTURE_CASE"})
            return {"batch_key": batch.batch_key, "case_count": len(batch.cases),
                    "existing_batch": existing_batch is not None,
                    "ready_for_new_import": not issues and existing_batch is None,
                    "issues": issues, "watermark": self.store.watermark(db)}
