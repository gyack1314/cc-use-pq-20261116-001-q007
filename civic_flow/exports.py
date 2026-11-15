"""为本地分析提供稳定且保护隐私的数据导出。"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from .contracts import DomainError
from .metrics import MetricsService
from .store import Store, digest

CASE_COLUMNS = (
    "case_id", "source", "external_id", "grid_id", "district", "category",
    "priority", "accepted_at", "department", "status", "resolution",
    "first_response_at", "response_due", "resolution_due", "version",
)

class ExportService:
    def __init__(self, store: Store):
        self.store = store
        self.calculator = MetricsService(store)

    @staticmethod
    def _page(after: int, limit: int) -> None:
        if after < 0 or not 1 <= limit <= 500:
            raise DomainError("BAD_PAGE", "Invalid extract cursor or limit")

    @staticmethod
    def _csv(rows: list[dict], fields: tuple[str, ...]) -> str:
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for item in rows:
            writer.writerow({key: item.get(key) for key in fields})
        return stream.getvalue()

    def cases(self, *, after: int = 0, limit: int = 100, district: str | None = None,
              category: str | None = None, fmt: str = "json") -> dict:
        self._page(after, limit)
        if fmt not in {"json", "csv"}:
            raise DomainError("BAD_FORMAT", "Extract format must be json or csv")
        with self.store.transaction() as db:
            watermark = self.store.watermark(db)
            source = self.store.case_list(db, district=district, category=category,
                                          after=after, limit=limit)
            rows = [{key: item[key] for key in CASE_COLUMNS} for item in source]
            cursor = rows[-1]["case_id"] if rows else None
            return {"watermark": watermark, "next_cursor": cursor,
                    "count": len(rows), "format": fmt,
                    "digest": digest(rows),
                    "data": rows if fmt == "json" else self._csv(rows, CASE_COLUMNS)}

    def metrics(self, *, as_of: datetime, group_by: str = "district",
                district: str | None = None, category: str | None = None,
                fmt: str = "json") -> dict:
        if fmt not in {"json", "csv"}:
            raise DomainError("BAD_FORMAT", "Extract format must be json or csv")
        with self.store.transaction() as db:
            result = self.calculator.calculate(db, as_of=as_of, group_by=group_by,
                                               district=district, category=category)
            if fmt == "json":
                data = result
            else:
                fields = ("key", "accepted", "open", "responded", "response_overdue",
                          "resolution_overdue", "closed", "solved", "acknowledged",
                          "response_rate", "closure_rate")
                data = self._csv(result["groups"], fields)
            return {"watermark": result["watermark"], "as_of": result["as_of"],
                    "format": fmt, "digest": digest(data), "data": data}
