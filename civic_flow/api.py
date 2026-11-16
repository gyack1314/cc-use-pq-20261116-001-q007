"""街区诉求受理与分析的 FastAPI 接口。"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .cases import CaseService
from .contracts import (AssociationInput, AssociationResolveInput,
                        AssociationScanInput, BatchInput, DomainError, EventInput,
                        GridInput, MergeUndoInput, ProblemCaseInput, ProblemInput,
                        ProblemMergeInput, ProblemSplitInput, ReportInput,
                        RouteRuleInput)
from .ingest import IntakeService
from .metrics import MetricsService
from .quality import QualityService
from .exports import ExportService
from .operations import OperationsService
from .diagnostics import DiagnosticsService
from .catalog import CatalogService
from .briefing import BriefingService
from .problems import ProblemService
from .screening import ScreeningService
from .reports import ReportService
from .routing import RoutingService
from .store import Store

def create_app(path: str | Path | None = None) -> FastAPI:
    database = path if path is not None else os.environ.get("CIVIC_FLOW_DB", "civic-flow.sqlite3")
    store = Store(database)
    routes = RoutingService(store)
    intake = IntakeService(store)
    cases = CaseService(store)
    metrics = MetricsService(store)
    reports = ReportService(store)
    quality = QualityService(store)
    exports = ExportService(store)
    operations = OperationsService(store)
    diagnostics = DiagnosticsService(store)
    catalog = CatalogService(store)
    briefing = BriefingService(store)
    problems = ProblemService(store)
    screening = ScreeningService(store)
    app = FastAPI(title="Civic Flow", version="1.0.0")
    app.state.store = store

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status, content={"code": exc.code,
                            "message": exc.message, "detail": exc.detail})

    @app.get("/health")
    def health():
        with store.connection() as db:
            return {"status": "ok", "watermark": store.watermark(db),
                    "counts": store.table_counts(db)}

    @app.put("/grids/{grid_id}")
    def put_grid(grid_id: str, body: GridInput, actor: str = Query(min_length=1)):
        if grid_id != body.grid_id:
            raise DomainError("GRID_ID_MISMATCH", "Grid path and body differ")
        return routes.put_grid(body, actor, datetime.now(timezone.utc))

    @app.get("/grids")
    def list_grids():
        return {"items": routes.list_grids()}

    @app.put("/routes/{category}")
    def put_route(category: str, body: RouteRuleInput, actor: str = Query(min_length=1)):
        if category != body.category:
            raise DomainError("CATEGORY_MISMATCH", "Category path and body differ")
        return routes.put_rule(body, actor, datetime.now(timezone.utc))

    @app.get("/routes")
    def list_routes():
        return {"items": routes.list_rules()}

    @app.get("/policies/{kind}/{key}/history")
    def policy_history(kind: str, key: str):
        return {"items": routes.history(kind, key)}

    @app.post("/batches")
    def import_batch(body: BatchInput):
        return intake.import_batch(body)

    @app.post("/batches/preflight")
    def preflight_batch(body: BatchInput):
        return screening.preflight(body)

    @app.get("/batches/{key}")
    def get_batch(key: str):
        return intake.get_batch(key)

    @app.get("/batches/{key}/trace")
    def batch_trace(key: str):
        return intake.batch_trace(key)

    @app.get("/intake/summary")
    def intake_summary():
        return intake.import_summary()

    @app.get("/cases")
    def list_cases(district: str | None = None, category: str | None = None,
                   status: str | None = None, after: int = 0, limit: int = 50):
        return cases.list(district, category, status, after, limit)

    @app.get("/cases/{case_id}")
    def get_case(case_id: int, history: bool = False):
        return cases.get(case_id, history=history)

    @app.post("/cases/{case_id}/events")
    def add_event(case_id: int, body: EventInput):
        return cases.append(case_id, body)

    @app.get("/cases/{case_id}/events")
    def case_events(case_id: int):
        return {"items": cases.history(case_id)}

    @app.get("/cases/{case_id}/timeline")
    def case_timeline(case_id: int):
        return cases.timeline(case_id)

    @app.get("/cases/{case_id}/at")
    def case_at(case_id: int, as_of: datetime):
        return metrics.case_at(case_id, as_of)

    @app.post("/problems")
    def open_problem(body: ProblemInput):
        return problems.open(body)

    @app.get("/problems")
    def list_problems(status: str | None = None, after: int = 0, limit: int = 50):
        return problems.list(status, after, limit)

    @app.get("/problems/{problem_id}")
    def get_problem(problem_id: int, as_of: datetime | None = None, history: bool = False):
        return problems.get(problem_id, as_of=as_of, history=history)

    @app.get("/problems/{problem_id}/cases")
    def problem_cases(problem_id: int, as_of: datetime | None = None):
        return problems.cases(problem_id, as_of=as_of)

    @app.get("/problems/{problem_id}/history")
    def problem_history(problem_id: int):
        return problems.history(problem_id)

    @app.post("/problems/{problem_id}/cases")
    def attach_problem_case(problem_id: int, body: ProblemCaseInput):
        return problems.attach(problem_id, body)

    @app.post("/problems/{problem_id}/split")
    def split_problem(problem_id: int, body: ProblemSplitInput):
        return problems.split(problem_id, body)

    @app.post("/problems/{problem_id}/merge")
    def merge_problem(problem_id: int, body: ProblemMergeInput):
        return problems.merge(problem_id, body)

    @app.post("/merges/{merge_id}/undo")
    def undo_merge(merge_id: int, body: MergeUndoInput):
        return problems.undo_merge(merge_id, body)

    @app.post("/associations")
    def suggest_association(body: AssociationInput):
        return problems.suggest(body)

    @app.post("/associations/scan")
    def scan_associations(body: AssociationScanInput):
        return problems.scan(body)

    @app.get("/associations")
    def list_associations(status: str | None = None, after: int = 0, limit: int = 50):
        return problems.suggestions(status, after, limit)

    @app.post("/associations/{suggestion_id}/confirm")
    def confirm_association(suggestion_id: int, body: AssociationResolveInput):
        return problems.resolve(suggestion_id, body)

    @app.post("/associations/{suggestion_id}/reject")
    def reject_association(suggestion_id: int, body: AssociationResolveInput):
        return problems.reject(suggestion_id, body)

    @app.get("/audit")
    def audit(after: int = 0, limit: int = 100):
        return cases.audit(after, limit)

    @app.get("/metrics")
    def metric_preview(as_of: datetime, group_by: str = "district", district: str | None = None,
                       category: str | None = None, include_cases: bool = False,
                       watermark: int | None = None):
        return metrics.preview(as_of=as_of, group_by=group_by, district=district,
                               category=category, include_cases=include_cases,
                               watermark=watermark)

    @app.post("/reports")
    def create_report(body: ReportInput):
        return reports.create(body)

    @app.get("/reports")
    def list_reports(after: str = "", limit: int = 50):
        return reports.list(after, limit)

    @app.get("/reports/{key}")
    def get_report(key: str):
        return reports.get(key)

    @app.get("/reports/{key}/verify")
    def verify_report(key: str):
        return reports.verify(key)

    @app.get("/reports/{key}/basis")
    def report_basis(key: str):
        return reports.basis(key)

    @app.get("/reports/{first}/compare/{second}")
    def compare_reports(first: str, second: str):
        return reports.compare(first, second)

    @app.get("/quality/inspect")
    def inspect_quality(as_of: datetime, limit: int = 200):
        return quality.inspect(as_of=as_of, limit=limit)

    @app.get("/quality/completeness")
    def completeness(as_of: datetime, district: str | None = None):
        return quality.completeness(as_of=as_of, district=district)

    @app.get("/quality/responsibility")
    def responsibility(as_of: datetime):
        return quality.responsibility_load(as_of=as_of)

    @app.get("/quality/coverage")
    def coverage(as_of: datetime):
        return quality.coverage(as_of=as_of)

    @app.get("/quality/problems")
    def problem_reconciliation(as_of: datetime):
        return quality.problem_reconciliation(as_of=as_of)

    @app.get("/exports/cases")
    def export_cases(after: int = 0, limit: int = 100, district: str | None = None,
                     category: str | None = None, fmt: str = "json"):
        return exports.cases(after=after, limit=limit, district=district, category=category, fmt=fmt)

    @app.get("/exports/metrics")
    def export_metrics(as_of: datetime, group_by: str = "district", district: str | None = None,
                       category: str | None = None, fmt: str = "json"):
        return exports.metrics(as_of=as_of, group_by=group_by, district=district,
                               category=category, fmt=fmt)

    @app.get("/operations/queue")
    def operations_queue(as_of: datetime, department: str | None = None,
                         district: str | None = None, include_closed: bool = False,
                         after: int = 0, limit: int = 50):
        return operations.queue(as_of=as_of, department=department, district=district,
                                include_closed=include_closed, after=after, limit=limit)

    @app.get("/operations/pressure")
    def operations_pressure(as_of: datetime, district: str | None = None):
        return operations.pressure(as_of=as_of, district=district)

    @app.get("/operations/aging")
    def operations_aging(as_of: datetime, district: str | None = None):
        return operations.aging(as_of=as_of, district=district)

    @app.get("/diagnostics/response-gap")
    def response_gap(as_of: datetime, group_by: str = "district", minimum_cases: int = 1):
        return diagnostics.response_gap(as_of=as_of, group_by=group_by,
                                        minimum_cases=minimum_cases)

    @app.get("/diagnostics/route-exposure")
    def route_exposure(as_of: datetime):
        return diagnostics.route_exposure(as_of=as_of)

    @app.get("/diagnostics/channel-mix")
    def channel_mix(as_of: datetime, district: str | None = None):
        return diagnostics.channel_mix(as_of=as_of, district=district)

    @app.get("/catalog")
    def catalog_overview():
        return catalog.overview()

    @app.get("/catalog/unconfigured")
    def catalog_unconfigured():
        return catalog.unconfigured()

    @app.get("/catalog/{kind}/{key}/impact")
    def catalog_impact(kind: str, key: str):
        return catalog.impact(kind=kind, key=key)

    @app.get("/catalog/{kind}/{key}/timeline")
    def catalog_timeline(kind: str, key: str):
        return catalog.timeline(kind=kind, key=key)

    @app.get("/briefing")
    def build_briefing(as_of: datetime, district: str | None = None):
        return briefing.build(as_of=as_of, district=district)

    return app

app = create_app()
