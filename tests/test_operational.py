import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from civic_flow.api import create_app

class OperationalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.tmp.name) / "db.sqlite3"))
        self.client.put("/grids/g1?actor=planner", json={"grid_id": "g1", "district": "A"})
        self.client.put("/grids/g2?actor=planner", json={"grid_id": "g2", "district": "B"})
        self.client.put("/routes/light?actor=planner", json={
            "category": "light", "department": "works", "response_hours": 24,
            "resolution_hours": 72})
        self.client.put("/routes/park?actor=planner", json={
            "category": "park", "department": "parks", "response_hours": 48,
            "resolution_hours": 96})
        cases = []
        for i, (grid, category, source) in enumerate((
                ("g1", "light", "resident"), ("g2", "park", "hotline"),
                ("g1", "park", "resident")), 1):
            cases.append({"external_id": str(i), "grid_id": grid, "category": category,
                          "summary": "Synthetic urban request", "resident_token": "person-" + str(i),
                          "source": source, "occurred_at": "2026-04-01T00:00:00Z"})
        self.client.post("/batches", json={"batch_key": "b", "submitted_at": "2026-04-02T00:00:00Z",
                                             "cases": cases})
        self.ids = [row["case_id"] for row in self.client.get("/cases").json()["items"]]
        self.as_of = "2026-04-04T00:00:00Z"

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def event(self, case_id, key, kind, version, at="2026-04-02T01:00:00Z", **extra):
        return self.client.post(f"/cases/{case_id}/events", json={
            "event_key": key, "kind": kind, "expected_version": version,
            "occurred_at": at, "actor": "worker", **extra})

    def test_quality_reconciles_valid_sample(self):
        result = self.client.get("/quality/inspect", params={"as_of": self.as_of}).json()
        self.assertEqual(result["issue_count"], 0)
        self.assertEqual(result["metrics_acceptance_count"], 3)
        self.assertEqual(result["counts"]["cases"], 3)

    def test_operational_queue_filters_department(self):
        result = self.client.get("/operations/queue", params={"as_of": self.as_of,
                                                               "department": "parks"}).json()
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(all(item["department"] == "parks" for item in result["items"]))

    def test_queue_marks_overdue(self):
        result = self.client.get("/operations/queue", params={"as_of": self.as_of}).json()
        self.assertEqual(len(result["items"]), 3)
        self.assertTrue(all(item["response_overdue"] for item in result["items"]))

    def test_queue_cursor_and_limit(self):
        first = self.client.get("/operations/queue", params={"as_of": self.as_of,
                                                             "limit": 1}).json()
        self.assertEqual(len(first["items"]), 1)
        self.assertTrue(first["has_more"])
        second = self.client.get("/operations/queue", params={"as_of": self.as_of,
            "after": first["next_cursor"], "limit": 1}).json()
        self.assertNotEqual(first["items"][0]["case_id"], second["items"][0]["case_id"])

    def test_pressure_ranks_overdue_departments(self):
        result = self.client.get("/operations/pressure", params={"as_of": self.as_of}).json()
        self.assertEqual(result["total_open"], 3)
        self.assertEqual(result["groups"][0]["department"], "parks")
        self.assertEqual(result["groups"][0]["open"], 2)

    def test_aging_buckets_sum_to_open(self):
        result = self.client.get("/operations/aging", params={"as_of": self.as_of}).json()
        self.assertEqual(sum(result["buckets"].values()), result["open_total"])
        self.assertEqual(result["buckets"]["three_to_seven_days"], 3)

    def test_catalog_policy_impact(self):
        result = self.client.get("/catalog/route/park/impact").json()
        self.assertEqual(result["case_count"], 2)
        self.assertEqual(result["open_count"], 2)
        self.assertEqual(result["policy"]["department"], "parks")

    def test_catalog_timeline_records_revision(self):
        self.client.put("/routes/park?actor=planner", json={
            "category": "park", "department": "parks-new", "response_hours": 48,
            "resolution_hours": 96})
        history = self.client.get("/catalog/route/park/timeline").json()
        self.assertEqual(history["current_revision"], 2)
        self.assertEqual(len(history["history"]), 2)

    def test_route_exposure_detects_prior_revision(self):
        self.client.put("/routes/park?actor=planner", json={
            "category": "park", "department": "parks-new", "response_hours": 48,
            "resolution_hours": 96})
        result = self.client.get("/diagnostics/route-exposure", params={"as_of": self.as_of}).json()
        self.assertEqual(result["affected_cases"], 2)

    def test_channel_mix_uses_accepted_source(self):
        result = self.client.get("/diagnostics/channel-mix", params={"as_of": self.as_of}).json()
        self.assertEqual({x["source"]: x["accepted"] for x in result["channels"]},
                         {"hotline": 1, "resident": 2})

    def test_response_gap_counts_unanswered(self):
        result = self.client.get("/diagnostics/response-gap", params={"as_of": self.as_of}).json()
        self.assertEqual(sum(x["awaiting_first_response"] for x in result["groups"]), 3)
        self.assertEqual(result["excluded_small_groups"], 0)

    def test_briefing_combines_business_views(self):
        result = self.client.get("/briefing", params={"as_of": self.as_of}).json()
        self.assertEqual(result["totals"]["accepted"], 3)
        self.assertEqual(result["aging"]["three_to_seven_days"], 3)
        self.assertTrue(any(x["code"] == "RESPONSE_OVERDUE" for x in result["signals"]))

    def test_export_excludes_private_fields(self):
        response = self.client.get("/exports/cases").json()
        self.assertEqual(response["count"], 3)
        self.assertNotIn("resident_hash", response["data"][0])
        self.assertNotIn("summary", response["data"][0])

    def test_export_csv_has_header(self):
        response = self.client.get("/exports/cases", params={"fmt": "csv"}).json()
        self.assertTrue(response["data"].startswith("case_id,source,external_id"))

    def test_metric_export_has_group_rows(self):
        response = self.client.get("/exports/metrics", params={"as_of": self.as_of,
                                                               "fmt": "csv"}).json()
        self.assertIn("accepted", response["data"])
        self.assertIn("A,2", response["data"])

    def test_audit_has_monotonic_journal(self):
        audit = self.client.get("/audit").json()
        sequences = [row["sequence"] for row in audit["items"]]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))

if __name__ == "__main__":
    unittest.main()
