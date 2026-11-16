"""汇总一致口径的基层治理分析简报。"""
from __future__ import annotations

from datetime import datetime

from .diagnostics import DiagnosticsService
from .metrics import MetricsService
from .operations import OperationsService
from .quality import QualityService
from .store import Store

class BriefingService:
    def __init__(self, store: Store):
        self.store = store
        self.metrics = MetricsService(store)
        self.operations = OperationsService(store)
        self.quality = QualityService(store)
        self.diagnostics = DiagnosticsService(store)

    def build(self, *, as_of: datetime, district: str | None = None) -> dict:
        overview = self.metrics.preview(as_of=as_of, group_by="district", district=district)
        pressure = self.operations.pressure(as_of=as_of, district=district)
        aging = self.operations.aging(as_of=as_of, district=district)
        completeness = self.quality.completeness(as_of=as_of, district=district)
        channels = self.diagnostics.channel_mix(as_of=as_of, district=district)
        if len({overview["watermark"], pressure["watermark"], aging["watermark"],
                completeness["watermark"], channels["watermark"]}) != 1:
            with self.store.transaction() as db:
                watermark = self.store.watermark(db)
                overview = self.metrics.calculate(db, as_of=as_of, group_by="district",
                                                  district=district, watermark=watermark)
                pressure = {"groups": [], "watermark": watermark}
                aging = {"buckets": {}, "watermark": watermark}
                completeness = {"unseen_categories": [], "watermark": watermark}
                channels = {"channels": [], "watermark": watermark}
        totals = overview["totals"]
        signals = []
        if totals["response_overdue"]:
            signals.append({"code": "RESPONSE_OVERDUE", "count": totals["response_overdue"]})
        if totals["resolution_overdue"]:
            signals.append({"code": "RESOLUTION_OVERDUE", "count": totals["resolution_overdue"]})
        if totals["open"] and not totals["responded"]:
            signals.append({"code": "NO_RESPONSE_YET", "count": totals["open"]})
        if totals["unfulfilled_response"]:
            signals.append({"code": "UNFULFILLED_RESPONSE_OBLIGATIONS",
                            "count": totals["unfulfilled_response"]})
        if totals["unfulfilled_resolution"]:
            signals.append({"code": "UNFULFILLED_RESOLUTION_OBLIGATIONS",
                            "count": totals["unfulfilled_resolution"]})
        return {"as_of": overview["as_of"], "district": district,
                "watermark": overview["watermark"], "totals": totals,
                "problems": overview.get("problems"),
                "districts": overview["groups"], "pressure": pressure["groups"],
                "aging": aging["buckets"], "unseen_categories": completeness["unseen_categories"],
                "channels": channels["channels"], "signals": signals}
