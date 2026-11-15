import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from civic_flow.api import create_app

class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.tmp.name) / "db.sqlite3"))
        for grid, district in (("north", "A"), ("south", "B")):
            self.client.put(f"/grids/{grid}?actor=admin", json={"grid_id": grid,
                                                                  "district": district})
        self.client.put("/routes/light?actor=admin", json={
            "category": "light", "department": "works", "response_hours": 24,
            "resolution_hours": 72})
        self.client.post("/batches", json={"batch_key": "b", "submitted_at": "2026-03-02T00:00:00Z",
            "cases": [{"external_id": str(i), "grid_id": grid, "category": "light",
                       "summary": "Synthetic lighting repair", "resident_token": "resident-" + str(i),
                       "occurred_at": "2026-03-01T00:00:00Z"}
                      for i, grid in enumerate(("north", "south"), 1)]})
        self.ids = [item["case_id"] for item in self.client.get("/cases").json()["items"]]

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def report(self, key, at="2026-03-04T00:00:00Z", **extra):
        return self.client.post("/reports", json={"report_key": key, "as_of": at,
                                                   "group_by": "district", **extra})

    def event(self, case_id, key, kind, version, at, **extra):
        return self.client.post(f"/cases/{case_id}/events", json={
            "event_key": key, "kind": kind, "expected_version": version,
            "actor": "worker", "occurred_at": at, **extra})

    def test_as_of_excludes_future_acceptance(self):
        response = self.report("early", "2026-02-28T00:00:00Z")
        self.assertEqual(response.json()["body"]["totals"]["accepted"], 0)
        self.assertEqual(response.json()["body"]["totals"]["response_rate"], None)

    def test_as_of_excludes_future_response(self):
        self.event(self.ids[0], "r", "responded", 1, "2026-03-03T00:00:00Z", note="Repair booked")
        earlier = self.report("early", "2026-03-02T00:00:00Z").json()["body"]
        later = self.report("late", "2026-03-04T00:00:00Z").json()["body"]
        self.assertEqual(earlier["totals"]["responded"], 0)
        self.assertEqual(later["totals"]["responded"], 1)

    def test_department_group_reflects_assignment_at_time(self):
        self.event(self.ids[0], "a", "assigned", 1, "2026-03-02T03:00:00Z",
                   department="parks")
        before = self.client.get("/metrics", params={"as_of": "2026-03-02T02:00:00Z",
                                                     "group_by": "department"}).json()
        after = self.client.get("/metrics", params={"as_of": "2026-03-02T04:00:00Z",
                                                    "group_by": "department"}).json()
        self.assertEqual([x["key"] for x in before["groups"]], ["works"])
        self.assertEqual([x["key"] for x in after["groups"]], ["parks", "works"])

    def test_overdue_response_and_resolution_are_distinct(self):
        snapshot = self.report("late", "2026-03-05T00:00:00Z").json()["body"]["totals"]
        self.assertEqual(snapshot["response_overdue"], 2)
        self.assertEqual(snapshot["resolution_overdue"], 2)
        self.assertEqual(snapshot["open"], 2)

    def test_explained_close_does_not_count_solved(self):
        self.event(self.ids[0], "e", "explained", 1, "2026-03-02T01:00:00Z",
                   note="Temporary closure")
        self.event(self.ids[0], "c", "closed", 2, "2026-03-02T02:00:00Z",
                   resolution="explained")
        totals = self.report("closed").json()["body"]["totals"]
        self.assertEqual(totals["solved"], 0)
        self.assertEqual(totals["explained_close"], 1)
        self.assertEqual(totals["closed"], 1)

    def test_reopen_changes_later_snapshot_only(self):
        self.event(self.ids[0], "r", "responded", 1, "2026-03-02T01:00:00Z", note="Answer")
        self.event(self.ids[0], "c", "closed", 2, "2026-03-02T02:00:00Z",
                   resolution="solved")
        before = self.report("before", "2026-03-02T03:00:00Z").json()
        self.event(self.ids[0], "o", "reopened", 3, "2026-03-03T01:00:00Z")
        after = self.report("after", "2026-03-04T00:00:00Z").json()
        self.assertEqual(before["body"]["totals"]["closed"], 1)
        self.assertEqual(after["body"]["totals"]["closed"], 0)
        self.assertEqual(after["body"]["totals"]["reopened"], 1)
        self.assertEqual(self.client.get("/reports/before").json()["body"], before["body"])

    def test_report_key_replay_and_conflict(self):
        first = self.report("stable").json()
        replay = self.report("stable").json()
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        changed = self.report("stable", "2026-03-06T00:00:00Z")
        self.assertEqual(changed.status_code, 409)

    def test_report_verify_and_compare(self):
        self.report("first", "2026-03-02T00:00:00Z")
        self.event(self.ids[0], "r", "responded", 1, "2026-03-03T00:00:00Z", note="Answer")
        self.report("second", "2026-03-04T00:00:00Z")
        compare = self.client.get("/reports/first/compare/second").json()
        self.assertEqual(compare["totals_delta"]["responded"], 1)
        self.assertTrue(self.client.get("/reports/first/verify").json()["matches"])

    def test_report_compare_rejects_scope_mismatch(self):
        self.report("all")
        self.report("one", district="A")
        response = self.client.get("/reports/all/compare/one")
        self.assertEqual(response.json()["code"], "REPORT_SCOPE_MISMATCH")

    def test_report_missing(self):
        response = self.client.get("/reports/unknown")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "REPORT_NOT_FOUND")

    def test_group_filter_limits_denominator(self):
        result = self.report("A", district="A").json()["body"]
        self.assertEqual(result["totals"]["accepted"], 1)
        self.assertEqual(result["groups"][0]["key"], "A")

if __name__ == "__main__":
    unittest.main()
