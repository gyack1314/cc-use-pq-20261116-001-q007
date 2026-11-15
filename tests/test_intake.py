import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from civic_flow.api import create_app

STAMP = "2026-01-10T12:00:00Z"
EARLY = "2026-01-09T08:00:00Z"

class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.tmp.name) / "db.sqlite3"))
        self.client.put("/grids/g1?actor=planner", json={"grid_id": "g1", "district": "D1"})
        self.client.put("/routes/light?actor=planner", json={
            "category": "light", "department": "works", "response_hours": 24,
            "resolution_hours": 96})

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def case(self, identity="a", grid="g1", category="light", when=EARLY):
        return {"external_id": identity, "grid_id": grid, "category": category,
                "summary": "Synthetic street issue", "occurred_at": when,
                "resident_token": "resident-" + identity}

    def batch(self, key="batch-a", cases=None):
        return {"batch_key": key, "submitted_at": STAMP,
                "cases": cases if cases is not None else [self.case()]}

    def test_atomic_intake_and_route_snapshot(self):
        response = self.client.post("/batches", json=self.batch())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["imported_count"], 1)
        item = self.client.get("/cases").json()["items"][0]
        self.assertEqual(item["department"], "works")
        self.assertEqual(item["district"], "D1")
        self.assertEqual(item["route_revision"], 1)
        self.assertEqual(item["response_due"], "2026-01-10T08:00:00+00:00")
        self.assertEqual(item["resolution_due"], "2026-01-13T08:00:00+00:00")

    def test_batch_replay_does_not_duplicate_case(self):
        first = self.client.post("/batches", json=self.batch()).json()
        replay = self.client.post("/batches", json=self.batch()).json()
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.client.get("/health").json()["counts"]["cases"], 1)

    def test_changed_batch_key_payload_conflicts(self):
        self.client.post("/batches", json=self.batch())
        changed = self.batch(cases=[self.case(identity="b")])
        response = self.client.post("/batches", json=changed)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "BATCH_KEY_CONFLICT")

    def test_source_identity_conflict_across_batches(self):
        self.client.post("/batches", json=self.batch())
        response = self.client.post("/batches", json=self.batch(key="next"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "CASE_ID_CONFLICT")

    def test_unknown_grid_rolls_back_whole_batch(self):
        response = self.client.post("/batches", json=self.batch(cases=[
            self.case(identity="a"), self.case(identity="b", grid="missing")]))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "GRID_UNKNOWN")
        self.assertEqual(self.client.get("/health").json()["counts"]["cases"], 0)
        self.assertEqual(self.client.get("/health").json()["counts"]["batches"], 0)

    def test_unknown_route_rolls_back_whole_batch(self):
        response = self.client.post("/batches", json=self.batch(cases=[
            self.case(identity="a"), self.case(identity="b", category="noise")]))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "ROUTE_UNKNOWN")
        self.assertEqual(self.client.get("/health").json()["counts"]["cases"], 0)

    def test_duplicate_identity_within_batch_is_rejected(self):
        response = self.client.post("/batches", json=self.batch(cases=[self.case(), self.case()]))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "BATCH_DUPLICATE")

    def test_future_case_is_rejected(self):
        response = self.client.post("/batches", json=self.batch(cases=[
            self.case(when="2026-01-11T00:00:00Z")]))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "FUTURE_CASE")

    def test_naive_timestamp_is_rejected(self):
        value = self.batch()
        value["cases"][0]["occurred_at"] = "2026-01-09T08:00:00"
        response = self.client.post("/batches", json=value)
        self.assertEqual(response.status_code, 422)

    def test_preflight_reports_multiple_reasons_without_writing(self):
        value = self.batch(cases=[self.case(grid="bad", category="noise"), self.case("b")])
        response = self.client.post("/batches/preflight", json=value)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({x["code"] for x in response.json()["issues"]},
                         {"GRID_UNKNOWN", "ROUTE_UNKNOWN"})
        self.assertEqual(self.client.get("/health").json()["counts"]["cases"], 0)

    def test_routing_update_affects_new_cases_only(self):
        self.client.post("/batches", json=self.batch())
        self.client.put("/routes/light?actor=planner", json={
            "category": "light", "department": "new_works", "response_hours": 48,
            "resolution_hours": 96})
        self.client.post("/batches", json=self.batch("batch-b", [self.case("b")]))
        items = self.client.get("/cases").json()["items"]
        self.assertEqual([x["department"] for x in items], ["works", "new_works"])
        self.assertEqual([x["route_revision"] for x in items], [1, 2])

    def test_inactive_grid_blocks_new_intake(self):
        self.client.put("/grids/g1?actor=planner", json={
            "grid_id": "g1", "district": "D1", "active": False})
        response = self.client.post("/batches", json=self.batch())
        self.assertEqual(response.json()["code"], "GRID_INACTIVE")

    def test_batch_lookup_missing(self):
        response = self.client.get("/batches/not-there")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "BATCH_NOT_FOUND")

if __name__ == "__main__":
    unittest.main()
