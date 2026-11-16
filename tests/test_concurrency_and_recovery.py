import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from civic_flow.api import create_app

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db.sqlite3"
        self.client = TestClient(create_app(self.path))
        self.client.put("/grids/g?actor=admin", json={"grid_id": "g", "district": "D"})
        self.client.put("/routes/repair?actor=admin", json={
            "category": "repair", "department": "team", "response_hours": 24,
            "resolution_hours": 72})
        self.batch = {"batch_key": "b1", "submitted_at": "2026-05-02T00:00:00Z",
                      "cases": [{"external_id": "one", "grid_id": "g", "category": "repair",
                                 "summary": "Synthetic repair ticket", "resident_token": "person-1",
                                 "occurred_at": "2026-05-01T00:00:00Z"}]}

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_reopen_database_preserves_case_and_report(self):
        self.assertEqual(self.client.post("/batches", json=self.batch).status_code, 200)
        self.client.post("/reports", json={"report_key": "r", "as_of": "2026-05-03T00:00:00Z"})
        with TestClient(create_app(self.path)) as later:
            self.assertEqual(later.get("/cases").json()["items"][0]["external_id"], "one")
            self.assertEqual(later.get("/reports/r").json()["body"]["totals"]["accepted"], 1)
            self.assertTrue(later.get("/reports/r/verify").json()["matches"])

    def test_failed_batch_then_corrected_batch_can_reuse_key(self):
        invalid = {**self.batch, "cases": [{**self.batch["cases"][0], "grid_id": "missing"}]}
        first = self.client.post("/batches", json=invalid)
        self.assertEqual(first.status_code, 422)
        corrected = self.client.post("/batches", json=self.batch)
        self.assertEqual(corrected.status_code, 200)
        self.assertFalse(corrected.json()["replayed"])

    def test_concurrent_same_batch_imports_once(self):
        def submit(_):
            with TestClient(create_app(self.path)) as other:
                response = other.post("/batches", json=self.batch)
                return response.status_code, response.json()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(submit, range(4)))
        self.assertEqual([status for status, _ in results], [200] * 4)
        self.assertEqual(sum(not data["replayed"] for _, data in results), 1)
        self.assertEqual(self.client.get("/health").json()["counts"]["cases"], 1)

    def test_competing_event_versions_allow_one_winner(self):
        self.client.post("/batches", json=self.batch)
        case_id = self.client.get("/cases").json()["items"][0]["case_id"]
        def submit(index):
            with TestClient(create_app(self.path)) as other:
                return other.post(f"/cases/{case_id}/events", json={
                    "event_key": "r-" + str(index), "kind": "responded",
                    "occurred_at": "2026-05-02T01:00:00Z", "actor": "team",
                    "expected_version": 1, "note": "Response " + str(index)}).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(submit, range(2)))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(self.client.get(f"/cases/{case_id}").json()["version"], 2)

    def test_report_freezes_watermark_when_new_case_arrives(self):
        self.client.post("/batches", json=self.batch)
        before = self.client.post("/reports", json={
            "report_key": "before", "as_of": "2026-05-04T00:00:00Z"}).json()
        second = {**self.batch, "batch_key": "b2", "cases": [
            {**self.batch["cases"][0], "external_id": "two", "resident_token": "person-2"}]}
        self.client.post("/batches", json=second)
        later = self.client.post("/reports", json={
            "report_key": "later", "as_of": "2026-05-04T00:00:00Z"}).json()
        self.assertEqual(before["body"]["totals"]["accepted"], 1)
        self.assertEqual(later["body"]["totals"]["accepted"], 2)
        self.assertTrue(self.client.get("/reports/before/verify").json()["matches"])

    def test_journal_cursor_survives_reopen(self):
        initial = self.client.get("/audit").json()
        self.client.post("/batches", json=self.batch)
        with TestClient(create_app(self.path)) as later:
            changes = later.get("/audit", params={"after": initial["watermark"]}).json()
            self.assertTrue(changes["items"])
            self.assertTrue(all(x["sequence"] > initial["watermark"] for x in changes["items"]))

    def test_case_listing_keyset_pagination(self):
        self.client.post("/batches", json=self.batch)
        second = {**self.batch, "batch_key": "b2", "cases": [
            {**self.batch["cases"][0], "external_id": "two", "resident_token": "person-2"}]}
        self.client.post("/batches", json=second)
        page = self.client.get("/cases", params={"limit": 1}).json()
        follow = self.client.get("/cases", params={"after": page["next_cursor"]}).json()
        self.assertEqual(page["items"][0]["external_id"], "one")
        self.assertEqual(follow["items"][0]["external_id"], "two")

    def test_quality_after_failed_event_has_no_gap(self):
        self.client.post("/batches", json=self.batch)
        case_id = self.client.get("/cases").json()["items"][0]["case_id"]
        denied = self.client.post(f"/cases/{case_id}/events", json={
            "event_key": "bad", "kind": "closed", "actor": "team",
            "expected_version": 1, "occurred_at": "2026-05-02T01:00:00Z",
            "resolution": "solved"})
        self.assertEqual(denied.status_code, 409)
        result = self.client.get("/quality/inspect", params={
            "as_of": "2026-05-03T00:00:00Z"}).json()
        self.assertEqual(result["issue_count"], 0)

    def problem_batch(self):
        self.assertEqual(self.client.post("/batches", json=self.batch).status_code, 200)
        second = {**self.batch, "batch_key": "b2", "cases": [
            {**self.batch["cases"][0], "external_id": "two", "resident_token": "person-2",
             "source": "hotline", "summary": "Synthetic repair ticket near the school"}]}
        self.assertEqual(self.client.post("/batches", json=second).status_code, 200)
        return [item["case_id"] for item in self.client.get("/cases").json()["items"]]

    def open_problem(self, case_ids):
        return self.client.post("/problems", json={
            "title": "Shared repair", "grid_id": "g", "category": "repair",
            "case_ids": case_ids, "occurred_at": "2026-05-02T01:00:00Z",
            "operation_key": "problem-1"}).json()

    def test_concurrent_identical_problem_open_creates_one_problem(self):
        ids = self.problem_batch()
        def submit(_):
            with TestClient(create_app(self.path)) as other:
                response = other.post("/problems", json={
                    "title": "Shared repair", "grid_id": "g", "category": "repair",
                    "case_ids": ids, "occurred_at": "2026-05-02T01:00:00Z",
                    "operation_key": "problem-1"})
                return response.status_code, response.json()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(submit, range(4)))
        self.assertEqual([status for status, _ in results], [200] * 4)
        self.assertEqual(len({data["problem_id"] for _, data in results}), 1)
        self.assertEqual(self.client.get("/problems").json()["items"][0]["member_count"], 2)

    def test_concurrent_confirm_of_same_hint_forms_one_problem(self):
        ids = self.problem_batch()
        hint = self.client.post("/associations", json={
            "case_id": ids[0], "other_case_id": ids[1],
            "occurred_at": "2026-05-02T01:00:00Z"}).json()
        def submit(_):
            with TestClient(create_app(self.path)) as other:
                response = other.post(
                    f"/associations/{hint['suggestion_id']}/confirm",
                    json={"occurred_at": "2026-05-02T02:00:00Z", "actor": "reviewer",
                          "expected_version": 1})
                return response.status_code, response.json()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, range(2)))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        metrics = self.client.get("/metrics", params={
            "as_of": "2026-05-04T00:00:00Z", "group_by": "problem"}).json()
        self.assertEqual(metrics["problems"]["governance_problems"], 1)
        self.assertEqual(metrics["problems"]["grouped_cases"], 2)

    def test_split_survives_reopen_with_membership_intervals(self):
        ids = self.problem_batch()
        problem = self.open_problem(ids)
        split = self.client.post(f"/problems/{problem['problem_id']}/split", json={
            "case_ids": [ids[1]], "occurred_at": "2026-05-02T03:00:00Z",
            "actor": "reviewer", "expected_version": problem["version"]}).json()
        with TestClient(create_app(self.path)) as later:
            source = later.get(f"/problems/{problem['problem_id']}").json()
            moved = later.get(f"/problems/{split['problem']['problem_id']}").json()
            self.assertEqual(source["case_ids"], [ids[0]])
            self.assertEqual(moved["case_ids"], [ids[1]])
            historic = later.get(f"/problems/{problem['problem_id']}", params={
                "as_of": "2026-05-02T02:00:00Z"}).json()
            self.assertEqual(historic["case_ids"], ids)

    def test_membership_watermark_is_stable_across_restart(self):
        ids = self.problem_batch()
        self.open_problem(ids)
        frozen = self.client.get("/metrics", params={
            "as_of": "2026-05-04T00:00:00Z", "group_by": "problem"}).json()
        with TestClient(create_app(self.path)) as later:
            replay = later.get("/metrics", params={
                "as_of": "2026-05-04T00:00:00Z", "group_by": "problem",
                "watermark": frozen["watermark"]}).json()
            self.assertEqual(replay, frozen)

if __name__ == "__main__":
    unittest.main()
