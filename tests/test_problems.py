import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

from civic_flow.api import create_app


class ProblemFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.tmp.name) / "db.sqlite3"))
        self.client.put("/grids/g1?actor=planner", json={"grid_id": "g1", "district": "D1"})
        self.client.put("/grids/g2?actor=planner", json={"grid_id": "g2", "district": "D2"})
        self.client.put("/routes/light?actor=planner", json={
            "category": "light", "department": "works", "response_hours": 24,
            "resolution_hours": 72})
        batch = {"batch_key": "b1", "submitted_at": "2026-02-02T00:00:00Z", "cases": [
            {"external_id": "r-1", "grid_id": "g1", "category": "light",
             "summary": "Street lamp outside 12 Oak Road has failed",
             "occurred_at": "2026-02-01T08:00:00Z", "resident_token": "resident-1",
             "source": "resident"},
            {"external_id": "h-1", "grid_id": "g1", "category": "light",
             "summary": "Street lamp outside 12 Oak Road has failed",
             "occurred_at": "2026-02-01T09:00:00Z", "resident_token": "resident-2",
             "source": "hotline"},
            {"external_id": "w-1", "grid_id": "g1", "category": "light",
             "summary": "Lamp at Oak Road still out, reported at window",
             "occurred_at": "2026-02-01T10:00:00Z", "resident_token": "resident-3",
             "source": "window"},
            {"external_id": "r-2", "grid_id": "g2", "category": "light",
             "summary": "Street lamp outside 12 Oak Road has failed",
             "occurred_at": "2026-02-01T11:00:00Z", "resident_token": "resident-4",
             "source": "resident"},
        ]}
        self.assertEqual(self.client.post("/batches", json=batch).status_code, 200)
        self.ids = [item["case_id"] for item in self.client.get("/cases").json()["items"]]

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------- helpers
    def event(self, case_id, key, kind, version, at, **extra):
        return self.client.post(f"/cases/{case_id}/events", json={
            "event_key": key, "kind": kind, "expected_version": version,
            "occurred_at": at, "actor": "works", **extra})

    def close_case(self, case_id, at="2026-02-02T03:00:00Z"):
        self.event(case_id, f"r-{case_id}", "responded", 1,
                   "2026-02-02T02:00:00Z", note="Repair completed")
        return self.event(case_id, f"c-{case_id}", "closed", 2, at,
                          resolution="solved")

    def open_problem(self, ids=None, at="2026-02-02T01:00:00Z", key="p-1"):
        response = self.client.post("/problems", json={
            "title": "Oak Road lamp failure", "grid_id": "g1", "category": "light",
            "case_ids": ids if ids is not None else self.ids[:2],
            "occurred_at": at, "operation_key": key})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def problems(self, at="2026-02-03T00:00:00Z", **params):
        return self.client.get("/metrics", params={
            "as_of": at, "group_by": "problem", **params}).json()

    def report(self, key, at, group_by="problem"):
        response = self.client.post("/reports", json={
            "report_key": key, "as_of": at, "group_by": group_by})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class GovernanceProblemTests(ProblemFixture):
    def test_every_resident_request_kept_as_its_own_case(self):
        self.open_problem()
        cases = self.client.get("/cases").json()["items"]
        self.assertEqual(len(cases), 4)
        self.assertEqual({item["source"] for item in cases},
                         {"resident", "hotline", "window"})

    def test_similar_content_is_only_a_hint_until_confirmed(self):
        scan = self.client.post("/associations/scan", json={
            "occurred_at": "2026-02-02T01:00:00Z", "grid_id": "g1"})
        self.assertEqual(scan.status_code, 200)
        hints = scan.json()["created"]
        self.assertTrue(hints)
        self.assertTrue(all(item["status"] == "pending" for item in hints))
        before = self.problems(at="2026-02-03T00:00:00Z")
        self.assertEqual(before["problems"]["governance_problems"], 0)
        self.assertEqual(before["problems"]["raw_cases"], 4)
        self.assertEqual(before["problems"]["grouped_cases"], 0)

        hint = hints[0]
        confirmed = self.client.post(f"/associations/{hint['suggestion_id']}/confirm",
                                     json={"occurred_at": "2026-02-02T02:00:00Z",
                                           "actor": "reviewer",
                                           "expected_version": hint["version"]})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        after = self.problems(at="2026-02-03T00:00:00Z")
        self.assertEqual(after["problems"]["raw_cases"], 4)
        self.assertEqual(after["problems"]["governance_problems"], 1)
        self.assertEqual(after["problems"]["grouped_cases"], 2)

    def test_hint_confirmation_is_idempotent_and_rejects_stale_version(self):
        suggestion = self.client.post("/associations", json={
            "case_id": self.ids[0], "other_case_id": self.ids[1],
            "occurred_at": "2026-02-02T01:00:00Z"}).json()
        replay = self.client.post("/associations", json={
            "case_id": self.ids[1], "other_case_id": self.ids[0],
            "occurred_at": "2026-02-02T01:30:00Z"}).json()
        self.assertTrue(replay["replayed"])
        first = self.client.post(f"/associations/{suggestion['suggestion_id']}/confirm",
                                 json={"occurred_at": "2026-02-02T02:00:00Z",
                                       "actor": "reviewer", "expected_version": 1})
        self.assertEqual(first.status_code, 200)
        stale = self.client.post(f"/associations/{suggestion['suggestion_id']}/reject",
                                 json={"occurred_at": "2026-02-02T03:00:00Z",
                                       "actor": "reviewer", "expected_version": 1})
        self.assertEqual(stale.json()["code"], "ASSOCIATION_CLOSED")

    def test_rejected_hint_never_becomes_a_problem(self):
        hint = self.client.post("/associations", json={
            "case_id": self.ids[0], "other_case_id": self.ids[1],
            "occurred_at": "2026-02-02T01:00:00Z"}).json()
        rejected = self.client.post(f"/associations/{hint['suggestion_id']}/reject",
                                    json={"occurred_at": "2026-02-02T02:00:00Z",
                                          "actor": "reviewer", "expected_version": 1})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(self.problems()["problems"]["governance_problems"], 0)

    def test_same_source_retransmission_is_idempotent_not_a_hint(self):
        duplicate = {"batch_key": "retry", "submitted_at": "2026-02-02T00:00:00Z",
                     "cases": [{"external_id": "r-1", "grid_id": "g1",
                                "category": "light",
                                "summary": "Street lamp outside 12 Oak Road has failed",
                                "occurred_at": "2026-02-01T08:00:00Z",
                                "resident_token": "resident-1", "source": "resident"}]}
        replay = self.client.post("/batches", json=duplicate)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()["code"], "CASE_ID_CONFLICT")
        scan = self.client.post("/associations/scan", json={
            "occurred_at": "2026-02-02T01:00:00Z"}).json()
        pairs = {tuple(sorted((x["case_low_id"], x["case_high_id"])))
                 for x in scan["created"]}
        self.assertNotIn((self.ids[0], self.ids[0]), pairs)
        # Cross-grid look-alikes are not offered as a shared problem either.
        self.assertFalse(any(self.ids[3] in pair for pair in pairs))

    def test_confirm_into_named_problem_keeps_other_resident_requests(self):
        problem = self.open_problem([self.ids[0]])
        hint = self.client.post("/associations", json={
            "case_id": self.ids[0], "other_case_id": self.ids[1],
            "occurred_at": "2026-02-02T01:30:00Z"}).json()
        self.client.post(f"/associations/{hint['suggestion_id']}/confirm",
                         json={"occurred_at": "2026-02-02T02:00:00Z",
                               "actor": "reviewer", "expected_version": 1,
                               "problem_id": problem["problem_id"]})
        members = self.client.get(f"/problems/{problem['problem_id']}/cases").json()
        self.assertEqual([item["case_id"] for item in members["items"]],
                         [self.ids[0], self.ids[1]])
        for item in members["items"]:
            self.assertIn("external_id", item)
            self.assertIn("source", item)


class MergeSplitTests(ProblemFixture):
    def test_split_moves_outstanding_obligations_not_completed_records(self):
        problem = self.open_problem(self.ids[:3])
        self.assertEqual(self.close_case(self.ids[0]).status_code, 200)
        split = self.client.post(f"/problems/{problem['problem_id']}/split", json={
            "case_ids": self.ids[:3], "occurred_at": "2026-02-02T04:00:00Z",
            "actor": "reviewer", "expected_version": problem["version"],
            "title": "Second lamp"})
        self.assertEqual(split.status_code, 200, split.text)
        body = split.json()
        self.assertEqual(body["retained_completed_case_ids"], [self.ids[0]])
        self.assertEqual(body["moved_case_ids"], self.ids[1:3])
        self.assertEqual(body["source"]["case_ids"], [self.ids[0]])
        self.assertEqual(body["problem"]["case_ids"], self.ids[1:3])
        completed = self.client.get(f"/cases/{self.ids[0]}").json()
        self.assertEqual(completed["status"], "closed")
        self.assertEqual(completed["version"], 3)

    def test_completed_request_stays_put_but_override_can_move_it(self):
        problem = self.open_problem(self.ids[:2])
        self.close_case(self.ids[0])
        denied = self.client.post(f"/problems/{problem['problem_id']}/split", json={
            "case_ids": [self.ids[0]], "occurred_at": "2026-02-02T04:00:00Z",
            "actor": "reviewer", "expected_version": problem["version"]})
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.json()["code"], "SPLIT_NO_OUTSTANDING_CASES")
        moved = self.client.post(f"/problems/{problem['problem_id']}/split", json={
            "case_ids": [self.ids[0]], "occurred_at": "2026-02-02T04:00:00Z",
            "actor": "reviewer", "expected_version": problem["version"],
            "move_completed": True})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["moved_case_ids"], [self.ids[0]])
        self.assertEqual(self.client.get(f"/cases/{self.ids[0]}").json()["status"],
                         "closed")

    def test_merge_and_undo_recompute_problem_counts(self):
        first = self.open_problem([self.ids[0]], key="p-a")
        second = self.client.post("/problems", json={
            "title": "Second lamp", "grid_id": "g1", "category": "light",
            "case_ids": self.ids[1:3], "occurred_at": "2026-02-02T01:05:00Z",
            "operation_key": "p-b"}).json()
        before = self.problems(at="2026-02-03T00:00:00Z")
        self.assertEqual(before["problems"]["governance_problems"], 2)
        merged = self.client.post(f"/problems/{second['problem_id']}/merge", json={
            "into_problem_id": first["problem_id"],
            "occurred_at": "2026-02-02T02:00:00Z", "actor": "reviewer",
            "expected_version": second["version"]})
        self.assertEqual(merged.status_code, 200, merged.text)
        after = self.problems(at="2026-02-03T00:00:00Z")
        self.assertEqual(after["problems"]["governance_problems"], 1)
        self.assertEqual(after["problems"]["raw_cases"], 4)
        self.assertEqual(after["problems"]["grouped_cases"], 3)
        undone = self.client.post(f"/merges/{merged.json()['merge']['merge_id']}/undo",
                                  json={"occurred_at": "2026-02-02T03:00:00Z",
                                        "actor": "reviewer"})
        self.assertEqual(undone.status_code, 200, undone.text)
        restored = self.problems(at="2026-02-03T00:00:00Z")
        self.assertEqual(restored["problems"]["governance_problems"], 2)
        self.assertEqual(restored["problems"]["grouped_cases"], 3)

    def test_undo_merge_keeps_cases_split_out_afterwards(self):
        first = self.open_problem([self.ids[0]], key="p-a")
        second = self.client.post("/problems", json={
            "title": "Second", "grid_id": "g1", "category": "light",
            "case_ids": self.ids[1:3], "occurred_at": "2026-02-02T01:05:00Z",
            "operation_key": "p-b"}).json()
        merged = self.client.post(f"/problems/{second['problem_id']}/merge", json={
            "into_problem_id": first["problem_id"],
            "occurred_at": "2026-02-02T02:00:00Z", "actor": "reviewer",
            "expected_version": second["version"]}).json()
        current = self.client.get(f"/problems/{first['problem_id']}").json()
        split = self.client.post(f"/problems/{first['problem_id']}/split", json={
            "case_ids": [self.ids[1]], "occurred_at": "2026-02-02T02:30:00Z",
            "actor": "reviewer", "expected_version": current["version"]}).json()
        undone = self.client.post(f"/merges/{merged['merge']['merge_id']}/undo",
                                  json={"occurred_at": "2026-02-02T03:00:00Z",
                                        "actor": "reviewer"})
        self.assertEqual(undone.status_code, 200, undone.text)
        self.assertEqual(undone.json()["restored_case_ids"], [self.ids[2]])
        split_out = self.client.get(f"/problems/{split['problem']['problem_id']}").json()
        self.assertEqual(split_out["case_ids"], [self.ids[1]])

    def test_undo_merge_returns_outstanding_obligations_only(self):
        first = self.open_problem([self.ids[0]], key="p-a")
        second = self.client.post("/problems", json={
            "title": "Second", "grid_id": "g1", "category": "light",
            "case_ids": self.ids[1:3], "occurred_at": "2026-02-02T01:05:00Z",
            "operation_key": "p-b"}).json()
        merged = self.client.post(f"/problems/{second['problem_id']}/merge", json={
            "into_problem_id": first["problem_id"],
            "occurred_at": "2026-02-02T02:00:00Z", "actor": "reviewer",
            "expected_version": second["version"]}).json()
        self.close_case(self.ids[1])
        undone = self.client.post(f"/merges/{merged['merge']['merge_id']}/undo",
                                  json={"occurred_at": "2026-02-02T05:00:00Z",
                                        "actor": "reviewer"}).json()
        self.assertEqual(undone["restored_case_ids"], [self.ids[2]])
        self.assertEqual(undone["retained_completed_case_ids"], [self.ids[1]])
        self.assertEqual(self.client.get(f"/cases/{self.ids[1]}").json()["problem_id"],
                         first["problem_id"])
        self.assertEqual(undone["restored"]["case_ids"], [self.ids[2]])

    def test_concurrent_response_and_split_never_misattributes(self):
        problem = self.open_problem(self.ids[:2])
        split = self.client.post(f"/problems/{problem['problem_id']}/split", json={
            "case_ids": [self.ids[1]], "occurred_at": "2026-02-02T04:00:00Z",
            "actor": "reviewer", "expected_version": problem["version"]}).json()
        new_problem = split["problem"]["problem_id"]
        late = self.event(self.ids[1], "late-response", "responded", 1,
                          "2026-02-02T02:00:00Z", note="Addressed")
        self.assertEqual(late.status_code, 200, late.text)
        self.assertEqual(late.json()["case"]["case_id"], self.ids[1])
        self.assertEqual(len(self.client.get(f"/cases/{self.ids[0]}/events").json()["items"]), 0)
        members = self.client.get(f"/problems/{new_problem}/cases").json()["items"]
        self.assertEqual([item["case_id"] for item in members], [self.ids[1]])
        self.assertEqual(members[0]["status"], "responded")


class MetricVersionTests(ProblemFixture):
    def test_metrics_separate_raw_cases_problems_and_obligations(self):
        problem = self.open_problem(self.ids[:3])
        self.close_case(self.ids[0])
        snapshot = self.problems(at="2026-02-03T00:00:00Z")
        self.assertEqual(snapshot["problems"]["raw_cases"], 4)
        self.assertEqual(snapshot["problems"]["governance_problems"], 1)
        self.assertEqual(snapshot["problems"]["grouped_cases"], 3)
        self.assertEqual(snapshot["problems"]["ungrouped_cases"], 1)
        self.assertEqual(snapshot["totals"]["accepted"], 4)
        self.assertEqual(snapshot["totals"]["unfulfilled_response"], 3)
        self.assertEqual(snapshot["totals"]["unfulfilled_resolution"], 3)

    def test_watermark_recomputes_before_and_after_association(self):
        before = self.client.get("/metrics", params={
            "as_of": "2026-02-03T00:00:00Z", "group_by": "problem"}).json()
        problem = self.open_problem(self.ids[:2])
        after = self.client.get("/metrics", params={
            "as_of": "2026-02-03T00:00:00Z", "group_by": "problem"}).json()
        replayed = self.client.get("/metrics", params={
            "as_of": "2026-02-03T00:00:00Z", "group_by": "problem",
            "watermark": before["watermark"]}).json()
        self.assertEqual(before["problems"]["governance_problems"], 0)
        self.assertEqual(after["problems"]["governance_problems"], 1)
        self.assertEqual(replayed, before)
        self.assertGreater(after["watermark"], before["watermark"])

    def test_as_of_ignores_association_made_later(self):
        problem = self.open_problem(self.ids[:2], at="2026-02-04T00:00:00Z")
        before = self.problems(at="2026-02-03T00:00:00Z")
        after = self.problems(at="2026-02-05T00:00:00Z")
        self.assertEqual(before["problems"]["governance_problems"], 0)
        self.assertEqual(before["problems"]["grouped_cases"], 0)
        self.assertEqual(after["problems"]["governance_problems"], 1)
        self.assertEqual(after["problems"]["grouped_cases"], 2)
        self.assertEqual(
            self.client.get(f"/problems/{problem['problem_id']}",
                            params={"as_of": "2026-02-03T00:00:00Z"}).json()["case_ids"],
            [])

    def test_invalid_watermark_is_rejected(self):
        response = self.client.get("/metrics", params={
            "as_of": "2026-02-03T00:00:00Z", "watermark": 99999})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "BAD_WATERMARK")

    def test_quality_reconciliation_reports_all_three_quantities(self):
        self.open_problem(self.ids[:2])
        self.client.post("/associations", json={
            "case_id": self.ids[0], "other_case_id": self.ids[1],
            "occurred_at": "2026-02-02T01:00:00Z"})
        result = self.client.get("/quality/problems", params={
            "as_of": "2026-02-03T00:00:00Z"}).json()
        self.assertEqual(result["raw_cases"], 4)
        self.assertEqual(result["raw_cases_by_source"],
                         {"hotline": 1, "resident": 2, "window": 1})
        self.assertEqual(result["governance_problems"], 1)
        self.assertEqual(result["grouped_cases"], 2)
        self.assertEqual(result["pending_association_count"], 1)
        self.assertIn("response", result["unfulfilled_obligations"])
        inspection = self.client.get("/quality/inspect", params={
            "as_of": "2026-02-03T00:00:00Z"}).json()
        self.assertEqual(inspection["issue_count"], 0)


class ReportBasisTests(ProblemFixture):
    def test_report_keeps_grouping_used_when_created(self):
        first = self.open_problem([self.ids[0]], key="p-a")
        second = self.client.post("/problems", json={
            "title": "Second", "grid_id": "g1", "category": "light",
            "case_ids": self.ids[1:3], "occurred_at": "2026-02-02T01:05:00Z",
            "operation_key": "p-b"}).json()
        frozen = self.report("before-merge", "2026-02-03T00:00:00Z")
        self.assertEqual(frozen["body"]["problems"]["governance_problems"], 2)
        self.assertEqual(frozen["grouping_basis"]["counted_problem_ids"],
                         [first["problem_id"], second["problem_id"]])
        self.client.post(f"/problems/{second['problem_id']}/merge", json={
            "into_problem_id": first["problem_id"],
            "occurred_at": "2026-02-04T00:00:00Z", "actor": "reviewer",
            "expected_version": second["version"]})
        self.assertTrue(self.client.get("/reports/before-merge/verify").json()["matches"])
        self.assertEqual(
            self.client.get("/reports/before-merge/basis").json()["grouping_basis"],
            frozen["grouping_basis"])
        self.assertEqual(self.client.get("/reports/before-merge").json()["body"],
                         frozen["body"])

    def test_later_report_uses_post_merge_grouping(self):
        first = self.open_problem([self.ids[0]], key="p-a")
        second = self.client.post("/problems", json={
            "title": "Second", "grid_id": "g1", "category": "light",
            "case_ids": self.ids[1:3], "occurred_at": "2026-02-02T01:05:00Z",
            "operation_key": "p-b"}).json()
        self.client.post(f"/problems/{second['problem_id']}/merge", json={
            "into_problem_id": first["problem_id"],
            "occurred_at": "2026-02-02T02:00:00Z", "actor": "reviewer",
            "expected_version": second["version"]})
        merged = self.report("after-merge", "2026-02-03T00:00:00Z")
        self.assertEqual(merged["body"]["problems"]["governance_problems"], 1)
        self.assertEqual(merged["grouping_basis"]["merged_problem_ids"],
                         {str(second["problem_id"]): first["problem_id"]})
        self.assertTrue(self.client.get("/reports/after-merge/verify").json()["matches"])

    def test_report_basis_records_pending_hints_as_uncounted(self):
        self.client.post("/associations", json={
            "case_id": self.ids[0], "other_case_id": self.ids[1],
            "occurred_at": "2026-02-02T01:00:00Z"})
        report = self.report("hints", "2026-02-03T00:00:00Z")
        basis = report["grouping_basis"]["associations"]
        self.assertEqual([item["status"] for item in basis], ["pending"])
        self.assertEqual(report["body"]["problems"]["governance_problems"], 0)


class ScopeTests(ProblemFixture):
    def test_association_across_grids_is_rejected(self):
        response = self.client.post("/associations", json={
            "case_id": self.ids[0], "other_case_id": self.ids[3],
            "occurred_at": "2026-02-02T01:00:00Z"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "ASSOCIATION_SCOPE_MISMATCH")

    def test_problem_rejects_case_from_other_grid(self):
        response = self.client.post("/problems", json={
            "title": "Mixed", "grid_id": "g1", "category": "light",
            "case_ids": [self.ids[0], self.ids[3]],
            "occurred_at": "2026-02-02T01:00:00Z"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "PROBLEM_SCOPE_MISMATCH")

    def test_case_cannot_belong_to_two_open_problems(self):
        first = self.open_problem([self.ids[0]], key="p-a")
        response = self.client.post("/problems", json={
            "title": "Duplicate", "grid_id": "g1", "category": "light",
            "case_ids": [self.ids[0]], "occurred_at": "2026-02-02T02:00:00Z",
            "operation_key": "p-b"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "CASE_ALREADY_GROUPED")
        self.assertEqual(self.client.get(f"/problems/{first['problem_id']}")
                         .json()["member_count"], 1)

    def test_hint_between_two_problems_requires_explicit_merge(self):
        self.open_problem([self.ids[0]], key="p-a")
        self.client.post("/problems", json={
            "title": "Second", "grid_id": "g1", "category": "light",
            "case_ids": [self.ids[1]], "occurred_at": "2026-02-02T01:05:00Z",
            "operation_key": "p-b"})
        hint = self.client.post("/associations", json={
            "case_id": self.ids[0], "other_case_id": self.ids[1],
            "occurred_at": "2026-02-02T01:30:00Z"}).json()
        denied = self.client.post(f"/associations/{hint['suggestion_id']}/confirm",
                                  json={"occurred_at": "2026-02-02T02:00:00Z",
                                        "actor": "reviewer", "expected_version": 1})
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.json()["code"], "ASSOCIATION_REQUIRES_MERGE")
        rejected = self.client.post(f"/associations/{hint['suggestion_id']}/reject",
                                    json={"occurred_at": "2026-02-02T02:00:00Z",
                                          "actor": "reviewer", "expected_version": 1})
        self.assertEqual(rejected.json()["suggestion"]["status"], "rejected")

    def test_problem_case_listing_respects_as_of(self):
        problem = self.open_problem(self.ids[:2], at="2026-02-02T01:00:00Z")
        before = self.client.get(f"/problems/{problem['problem_id']}/cases", params={
            "as_of": "2026-02-02T00:30:00Z"}).json()
        after = self.client.get(f"/problems/{problem['problem_id']}/cases").json()
        self.assertEqual(before["items"], [])
        self.assertEqual([item["problem_id"] for item in after["items"]],
                         [problem["problem_id"], problem["problem_id"]])

    def test_problem_open_is_idempotent_by_operation_key(self):
        first = self.open_problem([self.ids[0]], key="same")
        replay = self.open_problem([self.ids[0]], key="same")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["problem_id"], first["problem_id"])
        self.assertEqual(len(self.client.get("/problems").json()["items"]), 1)
        conflict = self.client.post("/problems", json={
            "title": "Different", "grid_id": "g1", "category": "light",
            "case_ids": [self.ids[0]], "occurred_at": "2026-02-02T01:00:00Z",
            "operation_key": "same"})
        self.assertEqual(conflict.json()["code"], "OPERATION_KEY_CONFLICT")

    def test_merged_problem_records_history_and_cannot_be_split(self):
        first = self.open_problem([self.ids[0]], key="p-a")
        second = self.client.post("/problems", json={
            "title": "Second", "grid_id": "g1", "category": "light",
            "case_ids": [self.ids[1]], "occurred_at": "2026-02-02T01:05:00Z",
            "operation_key": "p-b"}).json()
        self.client.post(f"/problems/{second['problem_id']}/merge", json={
            "into_problem_id": first["problem_id"],
            "occurred_at": "2026-02-02T02:00:00Z", "actor": "reviewer",
            "expected_version": second["version"]})
        history = self.client.get(f"/problems/{second['problem_id']}/history").json()
        self.assertTrue(history["merges"])
        self.assertTrue(history["events"])
        denied = self.client.post(f"/problems/{second['problem_id']}/split", json={
            "case_ids": [self.ids[1]], "occurred_at": "2026-02-02T03:00:00Z",
            "actor": "reviewer", "expected_version": 1})
        self.assertEqual(denied.json()["code"], "PROBLEM_MERGED")


if __name__ == "__main__":
    unittest.main()
