import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from civic_flow.api import create_app

class CaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.tmp.name) / "db.sqlite3"))
        self.client.put("/grids/g?actor=admin", json={"grid_id": "g", "district": "D"})
        self.client.put("/routes/repair?actor=admin", json={
            "category": "repair", "department": "team", "response_hours": 24,
            "resolution_hours": 72})
        self.client.post("/batches", json={"batch_key": "b", "submitted_at": "2026-02-02T00:00:00Z",
            "cases": [{"external_id": "c", "grid_id": "g", "category": "repair",
                       "summary": "Repair synthetic fixture", "resident_token": "sample-person",
                       "occurred_at": "2026-02-01T00:00:00Z"}]})
        self.case_id = self.client.get("/cases").json()["items"][0]["case_id"]

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def event(self, key, kind, version, at="2026-02-02T01:00:00Z", **extra):
        return {"event_key": key, "kind": kind, "actor": "worker",
                "expected_version": version, "occurred_at": at, **extra}

    def add(self, event):
        return self.client.post(f"/cases/{self.case_id}/events", json=event)

    def test_response_ack_close_lifecycle(self):
        response = self.add(self.event("r", "responded", 1, note="Repair booked"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["case"]["status"], "responded")
        ack = self.add(self.event("a", "acknowledged", 2, "2026-02-02T02:00:00Z"))
        self.assertEqual(ack.json()["case"]["status"], "acknowledged")
        close = self.add(self.event("x", "closed", 3, "2026-02-02T03:00:00Z",
                                    resolution="solved"))
        self.assertEqual(close.json()["case"]["resolution"], "solved")
        self.assertEqual(close.json()["case"]["version"], 4)
        self.assertEqual(len(self.client.get(f"/cases/{self.case_id}/events").json()["items"]), 3)

    def test_event_replay_is_idempotent_even_with_old_expected_version(self):
        event = self.event("r", "responded", 1, note="Repair booked")
        first = self.add(event)
        second = self.add(event)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(second.json()["case"]["version"], 2)

    def test_same_event_key_changed_content_conflicts(self):
        self.add(self.event("r", "responded", 1, note="First"))
        response = self.add(self.event("r", "responded", 1, note="Changed"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "EVENT_KEY_CONFLICT")

    def test_optimistic_version_rejects_stale_writer(self):
        self.add(self.event("r", "responded", 1, note="Answer"))
        response = self.add(self.event("another", "explained", 1, note="Explanation"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["actual"], 2)

    def test_time_order_rejects_backdated_update(self):
        self.add(self.event("r", "responded", 1, "2026-02-02T10:00:00Z", note="Answer"))
        response = self.add(self.event("a", "acknowledged", 2, "2026-02-02T09:00:00Z"))
        self.assertEqual(response.json()["code"], "EVENT_TIME_ORDER")
        self.assertEqual(self.client.get(f"/cases/{self.case_id}").json()["version"], 2)

    def test_cannot_close_without_response(self):
        response = self.add(self.event("c", "closed", 1, resolution="solved"))
        self.assertEqual(response.json()["code"], "CLOSE_BEFORE_RESPONSE")

    def test_cannot_ack_before_response(self):
        response = self.add(self.event("a", "acknowledged", 1))
        self.assertEqual(response.json()["code"], "ACK_BEFORE_RESPONSE")

    def test_explanation_cannot_claim_solution_without_followup(self):
        self.add(self.event("e", "explained", 1, note="Explained limits"))
        response = self.add(self.event("c", "closed", 2, resolution="solved"))
        self.assertEqual(response.json()["code"], "SOLUTION_MISMATCH")

    def test_explanation_can_close_as_explained(self):
        self.add(self.event("e", "explained", 1, note="Explained limits"))
        response = self.add(self.event("c", "closed", 2, resolution="explained"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["case"]["resolution"], "explained")

    def test_closed_case_only_reopens(self):
        self.add(self.event("r", "responded", 1, note="Answer"))
        self.add(self.event("c", "closed", 2, resolution="solved"))
        denied = self.add(self.event("again", "responded", 3, note="More"))
        self.assertEqual(denied.json()["code"], "CASE_CLOSED")
        reopened = self.add(self.event("open", "reopened", 3))
        self.assertEqual(reopened.status_code, 200)
        self.assertIsNone(reopened.json()["case"]["resolution"])

    def test_reopen_open_case_fails(self):
        response = self.add(self.event("open", "reopened", 1))
        self.assertEqual(response.json()["code"], "CASE_OPEN")

    def test_assignment_changes_current_owner(self):
        response = self.add(self.event("route", "assigned", 1, department="new_team"))
        self.assertEqual(response.json()["case"]["department"], "new_team")
        self.assertEqual(response.json()["case"]["status"], "assigned")

    def test_missing_case_returns_404(self):
        response = self.client.get("/cases/9999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "CASE_NOT_FOUND")

    def test_event_shape_validation(self):
        response = self.add(self.event("route", "assigned", 1))
        self.assertEqual(response.status_code, 422)
        response = self.add(self.event("answer", "responded", 1))
        self.assertEqual(response.status_code, 422)

    def test_history_exposes_journal_order(self):
        self.add(self.event("r", "responded", 1, note="Answer"))
        history = self.client.get(f"/cases/{self.case_id}?history=true").json()["events"]
        self.assertEqual(history[0]["event_key"], "r")
        self.assertEqual(history[0]["case_version"], 2)

if __name__ == "__main__":
    unittest.main()
