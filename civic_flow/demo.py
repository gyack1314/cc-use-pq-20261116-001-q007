"""使用本地合成数据运行离线验收。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from .api import create_app

def required(response, expected: int = 200) -> dict:
    if response.status_code != expected:
        raise RuntimeError(f"Expected HTTP {expected}, got {response.status_code}: {response.text}")
    return response.json()

def run() -> dict:
    with tempfile.TemporaryDirectory() as folder:
        client = TestClient(create_app(Path(folder) / "demo.sqlite3"))
        required(client.put("/grids/g-1?actor=analyst",
                            json={"grid_id": "g-1", "district": "north", "active": True}))
        required(client.put("/grids/g-2?actor=analyst",
                            json={"grid_id": "g-2", "district": "south", "active": True}))
        required(client.put("/routes/lighting?actor=analyst", json={
            "category": "lighting", "department": "public_works", "priority": 5,
            "response_hours": 24, "resolution_hours": 168}))
        required(client.put("/routes/fitness?actor=analyst", json={
            "category": "fitness", "department": "parks", "priority": 6,
            "response_hours": 48, "resolution_hours": 240}))
        batch = {"batch_key": "demo-001", "submitted_at": "2026-01-02T12:00:00Z", "cases": [
            {"external_id": "n-1", "grid_id": "g-1", "category": "lighting",
             "summary": "Street lamp has failed", "occurred_at": "2026-01-01T08:00:00Z",
             "resident_token": "synthetic-person-1", "priority": 1},
            {"external_id": "s-1", "grid_id": "g-2", "category": "fitness",
             "summary": "Outdoor exercise equipment is damaged",
             "occurred_at": "2026-01-01T09:00:00Z",
             "resident_token": "synthetic-person-2", "priority": 2},
        ]}
        imported = required(client.post("/batches", json=batch))
        if imported["imported_count"] != 2:
            raise RuntimeError("Synthetic batch count changed")
        again = required(client.post("/batches", json=batch))
        if not again["replayed"]:
            raise RuntimeError("Batch replay was not idempotent")
        items = required(client.get("/cases"))["items"]
        first = items[0]["case_id"]
        event = {"event_key": "answer-1", "kind": "responded",
                 "occurred_at": "2026-01-02T08:00:00Z", "actor": "public_works",
                 "expected_version": 1, "note": "Repair team scheduled"}
        required(client.post(f"/cases/{first}/events", json=event))
        required(client.post(f"/cases/{first}/events", json={
            "event_key": "close-1", "kind": "closed", "actor": "public_works",
            "occurred_at": "2026-01-03T08:00:00Z", "expected_version": 2,
            "resolution": "solved"}))
        before = required(client.post("/reports", json={
            "report_key": "before", "as_of": "2026-01-02T07:00:00Z",
            "group_by": "district"}))
        after = required(client.post("/reports", json={
            "report_key": "after", "as_of": "2026-01-04T00:00:00Z",
            "group_by": "district"}))
        if before["body"]["totals"]["closed"] != 0:
            raise RuntimeError("Historic snapshot contains future closure")
        if after["body"]["totals"]["closed"] != 1:
            raise RuntimeError("Closure missing from later snapshot")
        quality = required(client.get("/quality/inspect", params={
            "as_of": "2026-01-04T00:00:00Z"}))
        if quality["issue_count"]:
            raise RuntimeError("Synthetic data failed reconciliation")
        check = required(client.get("/reports/after/verify"))
        if not check["matches"]:
            raise RuntimeError("Frozen report verification failed")
        return {"accepted": imported["imported_count"],
                "closed_as_of_later_report": after["body"]["totals"]["closed"],
                "historic_closed": before["body"]["totals"]["closed"],
                "quality_issues": quality["issue_count"], "report_verified": check["matches"]}

if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
