"""离线检查源码语法和公开接口。"""
from __future__ import annotations

import ast
from pathlib import Path

from .api import create_app

def check() -> dict:
    root = Path(__file__).resolve().parent
    modules = [path for path in root.glob("*.py") if path.name != "__init__.py"]
    for path in modules:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    app = create_app(":memory:")
    routes = {(route.path, tuple(sorted(route.methods or ()))) for route in app.routes}
    required = {"/batches", "/cases/{case_id}/events", "/reports", "/metrics"}
    missing = required - {path for path, _ in routes}
    if missing:
        raise RuntimeError("Missing API routes: " + ", ".join(sorted(missing)))
    return {"parsed_modules": len(modules), "route_count": len(routes)}

if __name__ == "__main__":
    print(check())
