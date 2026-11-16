"""治理问题：把来源独立的诉求关联起来，而不吞掉任何一份诉求。

设计约束（与需求一一对应）：

* 每份诉求始终是 ``cases`` 里的一条独立记录；关联只通过追加式的
  ``problem_members`` 区间表达，从不改写或删除诉求本身。
* 相似内容只写入 ``association_suggestions``（线索）。确认之前它不影响
  任何治理问题量；确认后才开问题或把诉求并入指定问题。
* 合并、拆分、撤销合并都以追加事件表达；旧区间只做“结束标记”，
  新区间追加写。因此按水位重算历史报表仍保留当时的分组依据，按业务
  时点重算也能看到当时的分组。
* 一次回应永远绑定在具体 ``case_id`` 上，所以拆分与回应并发也不会
  把回应套到别人头上；拆分只移动尚未回应完的义务所依附的诉求记录，
  已办完的诉求记录原样保留（其问题归属也原样保留，除非再次被显式移动）。
* 幂等：同一操作键、同一内容的重复提交只回放首次结果。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from .contracts import (AssociationInput, AssociationResolveInput,
                        AssociationScanInput, DomainError, MergeUndoInput,
                        ProblemCaseInput, ProblemInput, ProblemMergeInput,
                        ProblemSplitInput, utc)
from .metrics import derived, reconstruct
from .store import Store, canonical, digest

STOPWORDS = {
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "has", "have",
    "had", "been", "it", "its", "this", "that", "there", "here", "for", "of",
    "to", "in", "on", "at", "by", "with", "from", "near", "street", "road",
    "issue", "problem", "please", "again", "still", "very", "not",
}

def keywords(*parts: str | None) -> set[str]:
    words: set[str] = set()
    for part in parts:
        for token in re.findall(r"[a-z0-9]+", (part or "").lower()):
            if len(token) > 1 and token not in STOPWORDS:
                words.add(token)
    return words

class ProblemService:
    def __init__(self, store: Store):
        self.store = store

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _case(db, case_id: int) -> dict:
        return Store.case(db, case_id)

    @staticmethod
    def _replay_operation(db, operation_key: str | None, operation: str,
                          payload: object) -> dict | None:
        if operation_key is None:
            return None
        prior = db.execute("SELECT * FROM problem_operations WHERE operation_key=?",
                           (operation_key,)).fetchone()
        if prior is None:
            return None
        if prior["operation"] != operation or prior["digest"] != digest(payload):
            raise DomainError("OPERATION_KEY_CONFLICT",
                              "Operation key has different content", 409)
        return json.loads(prior["result_json"])

    @staticmethod
    def _remember_operation(db, operation_key: str | None, operation: str,
                            payload: object, occurred_at: str, result: dict) -> None:
        if operation_key is None:
            return
        db.execute("INSERT INTO problem_operations VALUES(?,?,?,?,?)",
                   (operation_key, operation, digest(payload),
                    canonical(result), occurred_at))

    @staticmethod
    def _attach(db, problem_id: int, case_id: int, at: str, sequence: int,
                actor: str, origin: str) -> int:
        return int(db.execute(
            "INSERT INTO problem_members(problem_id,case_id,joined_at,joined_sequence,"
            "origin,origin_actor) VALUES(?,?,?,?,?,?)",
            (problem_id, case_id, at, sequence, origin, actor)).lastrowid)

    @staticmethod
    def _detach(db, membership: dict, at: str, sequence: int) -> None:
        if membership["left_sequence"] is not None:
            return
        db.execute("UPDATE problem_members SET left_at=?,left_sequence=? WHERE member_id=?",
                   (at, sequence, membership["member_id"]))

    @staticmethod
    def _open_problem(db, grid_id: str, category: str, department: str, title: str,
                      at: str, actor: str = "system", origin: str = "opened") -> int:
        grid = Store.grid(db, grid_id)
        if grid is None:
            raise DomainError("GRID_UNKNOWN", "Grid has no registered boundary", 422,
                              {"grid_id": grid_id})
        problem_id = int(db.execute(
            "INSERT INTO problems(grid_id,district,category,department,title,created_at,"
            "created_sequence,version) VALUES(?,?,?,?,?,?,0,1)",
            (grid_id, grid["district"], category, department, title, at)).lastrowid)
        sequence = Store.journal(db, "problem_opened", "problem", str(problem_id),
                                 {"actor": actor, "origin": origin, "grid_id": grid_id,
                                  "category": category, "department": department,
                                  "title": title}, at)
        # Journal rows are append-only; the sequence is assigned to the
        # problem afterwards instead of rewriting the audit entry.
        db.execute("UPDATE problems SET created_sequence=? WHERE problem_id=?",
                   (sequence, problem_id))
        return problem_id

    @staticmethod
    def _touch(db, problem_id: int) -> None:
        db.execute("UPDATE problems SET version=version+1 WHERE problem_id=?", (problem_id,))

    @staticmethod
    def _case_view(db, case: dict, watermark: int | None = None) -> dict:
        if watermark is None:
            membership = Store.current_membership(db, case["case_id"])
        else:
            membership = Store.membership_state(db, watermark).get(case["case_id"])
        return {"case_id": case["case_id"], "source": case["source"],
                "external_id": case["external_id"], "grid_id": case["grid_id"],
                "district": case["district"], "category": case["category"],
                "status": case["status"], "version": case["version"],
                "accepted_at": case["accepted_at"],
                "problem_id": membership["problem_id"] if membership else None}

    def _problem_view(self, db, problem: dict, watermark: int | None = None,
                      stamp: str | None = None) -> dict:
        members = Store.problem_members(db, problem["problem_id"], watermark, stamp)
        result = dict(problem)
        result["case_ids"] = [item["case_id"] for item in members]
        result["member_count"] = len(members)
        merge_state = Store.merge_state(db, watermark, stamp)
        result["merged_into"] = merge_state.get(problem["problem_id"])
        result["merged"] = result["merged_into"] is not None
        result["active"] = (result["merged_into"] is None
                            and problem["problem_id"] in
                            Store.active_problem_ids(db, watermark, stamp))
        if stamp is not None:
            result["as_of"] = stamp
        return result

    def _check_case_scope(self, problem: dict, case: dict) -> None:
        if (case["grid_id"] != problem["grid_id"]
                or case["category"] != problem["category"]):
            raise DomainError("PROBLEM_SCOPE_MISMATCH",
                              "Case does not belong to the problem scope", 409,
                              {"case_id": case["case_id"]})

    @staticmethod
    def _require_open(db, problem: dict) -> None:
        survivor = Store.merge_state(db).get(problem["problem_id"])
        if survivor is not None:
            raise DomainError("PROBLEM_MERGED", "Problem was merged elsewhere", 409,
                              {"merged_into": survivor})

    @staticmethod
    def _require_version(problem: dict, expected: int, label: str = "Problem") -> None:
        if problem["version"] != expected:
            raise DomainError("VERSION_CONFLICT", label + " version changed", 409,
                              {"expected": expected, "actual": problem["version"]})

    # ------------------------------------------------------------------ open
    def open(self, data: ProblemInput) -> dict:
        stamp = utc(data.occurred_at)
        with self.store.transaction() as db:
            grid = Store.grid(db, data.grid_id)
            if grid is None:
                raise DomainError("GRID_UNKNOWN", "Grid has no registered boundary", 422,
                                  {"grid_id": data.grid_id})
            if Store.rule(db, data.category) is None:
                raise DomainError("ROUTE_UNKNOWN", "Category has no responsibility rule", 422,
                                  {"category": data.category})
            if not data.case_ids:
                raise DomainError("PROBLEM_NEEDS_CASE",
                                  "A problem must start from at least one case", 422)
            payload = {"title": data.title, "grid_id": data.grid_id,
                       "category": data.category, "department": data.department,
                       "case_ids": data.case_ids, "occurred_at": stamp}
            replayed = self._replay_operation(db, data.operation_key, "problem_open", payload)
            if replayed is not None:
                return {**replayed, "replayed": True}
            seen: set[int] = set()
            cases = []
            for case_id in data.case_ids:
                if case_id in seen:
                    raise DomainError("PROBLEM_DUPLICATE_CASE", "Case appears twice", 409,
                                      {"case_id": case_id})
                seen.add(case_id)
                case = self._case(db, case_id)
                if case["grid_id"] != data.grid_id or case["category"] != data.category:
                    raise DomainError("PROBLEM_SCOPE_MISMATCH",
                                      "Case does not belong to the problem scope", 409,
                                      {"case_id": case_id})
                if Store.current_membership(db, case_id) is not None:
                    raise DomainError("CASE_ALREADY_GROUPED",
                                      "Case already belongs to a governance problem", 409,
                                      {"case_id": case_id})
                cases.append(case)
            department = data.department or cases[0]["department"]
            problem_id = self._open_problem(db, data.grid_id, data.category, department,
                                            data.title, stamp, data.actor, "opened")
            sequence = Store.problem(db, problem_id)["created_sequence"]
            for case in cases:
                self._attach(db, problem_id, case["case_id"], stamp, sequence,
                             data.actor, "opened")
            result = self._problem_view(db, Store.problem(db, problem_id))
            self._remember_operation(db, data.operation_key, "problem_open", payload,
                                     stamp, result)
            return {**result, "replayed": False}

    # -------------------------------------------------------------- membership
    def attach(self, problem_id: int, data: ProblemCaseInput) -> dict:
        stamp = utc(data.occurred_at)
        with self.store.transaction() as db:
            payload = {"problem_id": problem_id, "case_id": data.case_id,
                       "expected_version": data.expected_version, "actor": data.actor,
                       "occurred_at": stamp}
            replayed = self._replay_operation(db, data.operation_key,
                                              "problem_case_attach", payload)
            if replayed is not None:
                return {**replayed, "replayed": True}
            problem = Store.problem(db, problem_id)
            self._require_open(db, problem)
            self._require_version(problem, data.expected_version)
            case = self._case(db, data.case_id)
            self._check_case_scope(problem, case)
            if Store.current_membership(db, case["case_id"]) is not None:
                raise DomainError("CASE_ALREADY_GROUPED",
                                  "Case already belongs to a governance problem", 409,
                                  {"case_id": case["case_id"]})
            sequence = Store.journal(db, "problem_case_attached", "problem", str(problem_id),
                                     {"actor": data.actor, "case_id": case["case_id"]}, stamp)
            self._attach(db, problem_id, case["case_id"], stamp, sequence, data.actor,
                         "attached")
            self._touch(db, problem_id)
            result = self._problem_view(db, Store.problem(db, problem_id))
            self._remember_operation(db, data.operation_key, "problem_case_attach",
                                     payload, stamp, result)
            return {**result, "replayed": False}

    # ------------------------------------------------------- associations
    def suggest(self, data: AssociationInput) -> dict:
        """Record a reviewable hint. It never groups or counts anything."""
        stamp = utc(data.occurred_at)
        low, high = sorted((data.case_id, data.other_case_id))
        with self.store.transaction() as db:
            first, second = self._case(db, low), self._case(db, high)
            if first["grid_id"] != second["grid_id"]:
                raise DomainError("ASSOCIATION_SCOPE_MISMATCH",
                                  "Associated cases must share a grid", 409)
            prior = Store.suggestion_by_pair(db, low, high, data.reason)
            if prior is not None:
                if prior["status"] == "pending":
                    return {**prior, "replayed": True}
                raise DomainError("ASSOCIATION_CLOSED",
                                  "Association suggestion was already resolved", 409,
                                  {"suggestion_id": prior["suggestion_id"],
                                   "status": prior["status"]})
            suggestion_id = int(db.execute(
                "INSERT INTO association_suggestions(case_low_id,case_high_id,reason,"
                "similarity,status,created_at,created_sequence,version) "
                "VALUES(?,?,?,?,'pending',?,0,1)",
                (low, high, data.reason, data.similarity, stamp)).lastrowid)
            sequence = Store.journal(db, "association_suggested", "association",
                                     str(suggestion_id),
                                     {"actor": data.actor, "reason": data.reason,
                                      "similarity": data.similarity,
                                      "case_low_id": low, "case_high_id": high}, stamp)
            db.execute("UPDATE association_suggestions SET created_sequence=? "
                       "WHERE suggestion_id=?", (sequence, suggestion_id))
            return {**Store.suggestion(db, suggestion_id), "replayed": False}

    def scan(self, data: AssociationScanInput) -> dict:
        """Find cross-source similarity; only pending suggestions are written."""
        stamp = utc(data.occurred_at)
        created: list[dict] = []
        replayed = 0
        with self.store.transaction() as db:
            watermark = Store.watermark(db)
            clauses = ["created_sequence<=?"]
            params: list[object] = [watermark]
            if data.grid_id is not None:
                clauses.append("grid_id=?")
                params.append(data.grid_id)
            if data.category is not None:
                clauses.append("category=?")
                params.append(data.category)
            rows = [dict(item) for item in db.execute(
                "SELECT * FROM cases WHERE " + " AND ".join(clauses) + " ORDER BY case_id",
                params)]
            state = Store.membership_state(db)
            for index, left in enumerate(rows):
                left_words = keywords(left["summary"])
                for right in rows[index + 1:]:
                    if left["grid_id"] != right["grid_id"]:
                        continue
                    if left["source"] == right["source"]:
                        continue
                    right_words = keywords(right["summary"])
                    union = left_words | right_words
                    if not union:
                        continue
                    similarity = len(left_words & right_words) / len(union)
                    if similarity < data.threshold:
                        continue
                    low, high = sorted((left["case_id"], right["case_id"]))
                    low_problem = state.get(low, {}).get("problem_id")
                    high_problem = state.get(high, {}).get("problem_id")
                    if low_problem is not None and low_problem == high_problem:
                        continue
                    prior = Store.suggestion_by_pair(db, low, high, "similar_content")
                    if prior is not None:
                        replayed += 1
                        continue
                    suggestion_id = int(db.execute(
                        "INSERT INTO association_suggestions(case_low_id,case_high_id,reason,"
                        "similarity,status,created_at,created_sequence,version) "
                        "VALUES(?,?,?,?,'pending',?,0,1)",
                        (low, high, "similar_content", round(similarity, 4),
                         stamp)).lastrowid)
                    sequence = Store.journal(db, "association_suggested", "association",
                                             str(suggestion_id),
                                             {"actor": data.actor, "reason": "similar_content",
                                              "similarity": round(similarity, 4),
                                              "case_low_id": low, "case_high_id": high}, stamp)
                    db.execute("UPDATE association_suggestions SET created_sequence=? "
                               "WHERE suggestion_id=?", (sequence, suggestion_id))
                    created.append(Store.suggestion(db, suggestion_id))
            return {"as_of": stamp, "threshold": data.threshold,
                    "created": created, "created_count": len(created),
                    "already_suggested": replayed,
                    "watermark": Store.watermark(db)}

    def suggestions(self, status: str | None = None, after: int = 0, limit: int = 50) -> dict:
        if status is not None and status not in {"pending", "confirmed", "rejected"}:
            raise DomainError("BAD_STATUS", "Unknown suggestion status")
        if after < 0 or not 1 <= limit <= 200:
            raise DomainError("BAD_PAGE", "Invalid suggestion page")
        with self.store.transaction() as db:
            clauses, params = ["suggestion_id>?"], [after]
            if status is not None:
                clauses.append("status=?")
                params.append(status)
            params.append(limit)
            rows = [dict(item) for item in db.execute(
                "SELECT * FROM association_suggestions WHERE " + " AND ".join(clauses) +
                " ORDER BY suggestion_id LIMIT ?", params)]
            return {"items": rows, "watermark": Store.watermark(db),
                    "next_cursor": rows[-1]["suggestion_id"] if rows else None}

    def _require_pending(self, suggestion: dict, expected: int) -> None:
        if suggestion["status"] != "pending":
            raise DomainError("ASSOCIATION_CLOSED", "Suggestion was already resolved", 409,
                              {"status": suggestion["status"]})
        if suggestion["version"] != expected:
            raise DomainError("VERSION_CONFLICT", "Suggestion version changed", 409,
                              {"expected": expected, "actual": suggestion["version"]})

    def resolve(self, suggestion_id: int, data: AssociationResolveInput) -> dict:
        """Confirmation opens/extends a problem; only then does it count."""
        stamp = utc(data.occurred_at)
        with self.store.transaction() as db:
            payload = {"suggestion_id": suggestion_id, "problem_id": data.problem_id,
                       "actor": data.actor, "expected_version": data.expected_version,
                       "occurred_at": stamp}
            replayed = self._replay_operation(db, data.operation_key,
                                              "association_confirm", payload)
            if replayed is not None:
                return {**replayed, "replayed": True}
            suggestion = Store.suggestion(db, suggestion_id)
            self._require_pending(suggestion, data.expected_version)
            low = self._case(db, suggestion["case_low_id"])
            high = self._case(db, suggestion["case_high_id"])
            state = Store.membership_state(db)
            low_problem = state.get(low["case_id"], {}).get("problem_id")
            high_problem = state.get(high["case_id"], {}).get("problem_id")
            problem_id = data.problem_id
            if low_problem is not None and high_problem is not None:
                if low_problem == high_problem:
                    raise DomainError("NOTHING_TO_ASSOCIATE",
                                      "Cases already share a governance problem", 409,
                                      {"problem_id": low_problem})
                # Two different problems must be merged explicitly so that the
                # survivor and audit trail are a deliberate choice.
                raise DomainError("ASSOCIATION_REQUIRES_MERGE",
                                  "Cases sit in different problems; merge them", 409,
                                  {"problem_ids": sorted({low_problem, high_problem})})
            implied = low_problem or high_problem
            if problem_id is not None and implied is not None and problem_id != implied:
                raise DomainError("PROBLEM_SCOPE_MISMATCH",
                                  "Requested problem differs from current membership", 409)
            if problem_id is None and implied is not None:
                problem_id = implied
            if problem_id is not None:
                problem = Store.problem(db, problem_id)
                self._require_open(db, problem)
                self._check_case_scope(problem, low)
                self._check_case_scope(problem, high)
            pending: list[dict] = []
            if problem_id is None:
                problem_id = self._open_problem(db, low["grid_id"], low["category"],
                                                low["department"], low["summary"][:200],
                                                stamp, data.actor, "association")
                pending = [low, high]
            else:
                pending = [case for case, owner in ((low, low_problem), (high, high_problem))
                           if owner is None]
            sequence = Store.journal(db, "association_confirmed", "association",
                                     str(suggestion_id),
                                     {"actor": data.actor, "problem_id": problem_id,
                                      "case_ids": [case["case_id"] for case in pending]},
                                     stamp)
            for case in pending:
                current = Store.current_membership(db, case["case_id"])
                if current is not None:
                    if current["problem_id"] == problem_id:
                        continue
                    self._detach(db, current, stamp, sequence)
                self._attach(db, problem_id, case["case_id"], stamp, sequence,
                             data.actor, "association")
            db.execute("UPDATE association_suggestions SET status='confirmed',"
                       "resolved_at=?,resolved_sequence=?,confirmed_problem_id=?,"
                       "version=version+1 WHERE suggestion_id=?",
                       (stamp, sequence, problem_id, suggestion_id))
            self._touch(db, problem_id)
            result = {"suggestion": Store.suggestion(db, suggestion_id),
                      "problem": self._problem_view(db, Store.problem(db, problem_id)),
                      "watermark": Store.watermark(db)}
            self._remember_operation(db, data.operation_key, "association_confirm",
                                     payload, stamp, result)
            return result

    def reject(self, suggestion_id: int, data: AssociationResolveInput) -> dict:
        stamp = utc(data.occurred_at)
        with self.store.transaction() as db:
            payload = {"suggestion_id": suggestion_id, "actor": data.actor,
                       "expected_version": data.expected_version, "occurred_at": stamp,
                       "problem_id": data.problem_id}
            replayed = self._replay_operation(db, data.operation_key,
                                              "association_reject", payload)
            if replayed is not None:
                return {**replayed, "replayed": True}
            suggestion = Store.suggestion(db, suggestion_id)
            self._require_pending(suggestion, data.expected_version)
            sequence = Store.journal(db, "association_rejected", "association",
                                     str(suggestion_id), {"actor": data.actor}, stamp)
            db.execute("UPDATE association_suggestions SET status='rejected',resolved_at=?,"
                       "resolved_sequence=?,version=version+1 WHERE suggestion_id=?",
                       (stamp, sequence, suggestion_id))
            result = {"suggestion": Store.suggestion(db, suggestion_id),
                      "watermark": Store.watermark(db)}
            self._remember_operation(db, data.operation_key, "association_reject",
                                     payload, stamp, result)
            return result

    # ---------------------------------------------------------------- merges
    def merge(self, problem_id: int, data: ProblemMergeInput) -> dict:
        """Move membership intervals to the survivor; cases are never rewritten."""
        stamp = utc(data.occurred_at)
        if problem_id == data.into_problem_id:
            raise DomainError("MERGE_SELF", "A problem cannot merge into itself", 422)
        with self.store.transaction() as db:
            payload = {"problem_id": problem_id, "into_problem_id": data.into_problem_id,
                       "actor": data.actor, "expected_version": data.expected_version,
                       "expected_target_version": data.expected_target_version,
                       "suggestion_id": data.suggestion_id, "occurred_at": stamp}
            replayed = self._replay_operation(db, data.operation_key,
                                              "problem_merge", payload)
            if replayed is not None:
                return {**replayed, "replayed": True}
            source = Store.problem(db, problem_id)
            target = Store.problem(db, data.into_problem_id)
            self._require_open(db, source)
            self._require_open(db, target)
            self._require_version(source, data.expected_version)
            if data.expected_target_version is not None:
                self._require_version(target, data.expected_target_version, "Target problem")
            if (source["grid_id"], source["category"]) != (target["grid_id"], target["category"]):
                raise DomainError("PROBLEM_SCOPE_MISMATCH",
                                  "Only same-scope problems can merge", 409)
            members = Store.problem_members(db, source["problem_id"])
            if data.suggestion_id is not None:
                suggestion = Store.suggestion(db, data.suggestion_id)
                pair = {suggestion["case_low_id"], suggestion["case_high_id"]}
                if not pair & {item["case_id"] for item in members}:
                    raise DomainError("ASSOCIATION_MISMATCH",
                                      "Suggestion does not touch the merged problem", 409)
            sequence = Store.journal(db, "problems_merged", "problem", str(problem_id),
                                     {"actor": data.actor,
                                      "into_problem_id": target["problem_id"],
                                      "case_ids": [item["case_id"] for item in members],
                                      "suggestion_id": data.suggestion_id}, stamp)
            merge_id = int(db.execute(
                "INSERT INTO problem_merges(survivor_problem_id,merged_problem_id,"
                "suggestion_id,actor,occurred_at,merge_sequence) VALUES(?,?,?,?,?,?)",
                (target["problem_id"], problem_id, data.suggestion_id, data.actor,
                 stamp, sequence)).lastrowid)
            for item in members:
                db.execute("INSERT INTO merge_moves(merge_id,case_id,from_problem_id,"
                           "to_problem_id) VALUES(?,?,?,?)",
                           (merge_id, item["case_id"], problem_id, target["problem_id"]))
                self._detach(db, item, stamp, sequence)
                self._attach(db, target["problem_id"], item["case_id"], stamp, sequence,
                             data.actor, "merge")
            self._touch(db, problem_id)
            self._touch(db, target["problem_id"])
            if data.suggestion_id is not None:
                db.execute("UPDATE association_suggestions SET status='confirmed',"
                           "resolved_at=?,resolved_sequence=?,confirmed_problem_id=?,"
                           "version=version+1 WHERE suggestion_id=? AND status='pending'",
                           (stamp, sequence, target["problem_id"], data.suggestion_id))
            result = {"merge": Store.merge(db, merge_id),
                      "problem": self._problem_view(
                          db, Store.problem(db, target["problem_id"])),
                      "merged_problem": Store.problem(db, problem_id),
                      "watermark": Store.watermark(db)}
            self._remember_operation(db, data.operation_key, "problem_merge",
                                     payload, stamp, result)
            return result

    def undo_merge(self, merge_id: int, data: MergeUndoInput) -> dict:
        """Reopen the merged problem and return outstanding obligations to it.

        A resident whose request was completed under the survivor keeps that
        record and its current problem by default; only obligations still in
        flight move back, matching the rule used by splits.
        """
        stamp = utc(data.occurred_at)
        with self.store.transaction() as db:
            payload = {"merge_id": merge_id, "actor": data.actor, "occurred_at": stamp,
                       "move_completed": data.move_completed}
            replayed = self._replay_operation(db, data.operation_key,
                                              "merge_undo", payload)
            if replayed is not None:
                return {**replayed, "replayed": True}
            merge = Store.merge(db, merge_id)
            if merge["reversed_sequence"] is not None:
                raise DomainError("MERGE_ALREADY_REVERSED", "Merge was already reversed", 409)
            watermark = Store.watermark(db)
            sequence = Store.journal(db, "merge_reversed", "merge", str(merge_id),
                                     {"actor": data.actor,
                                      "survivor_problem_id": merge["survivor_problem_id"],
                                      "merged_problem_id": merge["merged_problem_id"],
                                      "move_completed": data.move_completed}, stamp)
            moved = [dict(item) for item in db.execute(
                "SELECT * FROM merge_moves WHERE merge_id=? ORDER BY case_id", (merge_id,))]
            restored, retained = [], []
            for item in moved:
                current = Store.current_membership(db, item["case_id"])
                if current is None or current["problem_id"] != merge["survivor_problem_id"]:
                    # Split out or re-homed afterwards: completed resident
                    # obligations keep the record they were handled under.
                    continue
                case = Store.case(db, item["case_id"])
                if not data.move_completed and self._completed(db, case, stamp, watermark):
                    retained.append(item["case_id"])
                    continue
                self._detach(db, current, stamp, sequence)
                self._attach(db, item["from_problem_id"], item["case_id"], stamp, sequence,
                             data.actor, "merge_undo")
                restored.append(item["case_id"])
            db.execute("UPDATE problem_merges SET reversed_at=?,reversed_sequence=? "
                       "WHERE merge_id=?", (stamp, sequence, merge_id))
            self._touch(db, merge["merged_problem_id"])
            self._touch(db, merge["survivor_problem_id"])
            result = {"merge": Store.merge(db, merge_id),
                      "restored": self._problem_view(
                          db, Store.problem(db, merge["merged_problem_id"])),
                      "survivor": self._problem_view(
                          db, Store.problem(db, merge["survivor_problem_id"])),
                      "restored_case_ids": restored,
                      "retained_completed_case_ids": retained,
                      "watermark": Store.watermark(db)}
            self._remember_operation(db, data.operation_key, "merge_undo",
                                     payload, stamp, result)
            return result

    # ------------------------------------------------------------------ split
    def _completed(self, db, case: dict, stamp: str, watermark: int) -> bool:
        """Was every resident obligation of this request fulfilled by stamp?"""
        rebuilt = reconstruct(case, Store.events(db, case["case_id"], watermark), stamp)
        return derived(rebuilt, stamp)["closed"]

    def split(self, problem_id: int, data: ProblemSplitInput) -> dict:
        """Split outstanding obligations out; completed requests stay put.

        A closed request is never moved by a split unless the caller opts in,
        so its record (and the problem it was handled under) is preserved.
        The rest of the split cases — the still-outstanding obligations —
        continue under the newly created problem.  Responses are always
        recorded against a ``case_id``, so a response racing with the split
        can never be attributed to another resident's request.
        """
        stamp = utc(data.occurred_at)
        with self.store.transaction() as db:
            payload = {"problem_id": problem_id, "case_ids": data.case_ids,
                       "actor": data.actor, "expected_version": data.expected_version,
                       "title": data.title, "department": data.department,
                       "move_completed": data.move_completed, "occurred_at": stamp}
            replayed = self._replay_operation(db, data.operation_key,
                                              "problem_split", payload)
            if replayed is not None:
                return {**replayed, "replayed": True}
            problem = Store.problem(db, problem_id)
            self._require_open(db, problem)
            self._require_version(problem, data.expected_version)
            if len(set(data.case_ids)) != len(data.case_ids):
                raise DomainError("PROBLEM_DUPLICATE_CASE", "Case appears twice", 409)
            watermark = Store.watermark(db)
            members = {item["case_id"]: item for item in
                       Store.problem_members(db, problem_id, watermark)}
            for case_id in data.case_ids:
                if case_id not in members:
                    raise DomainError("CASE_NOT_IN_PROBLEM",
                                      "Case is not an active member", 409,
                                      {"case_id": case_id})
            moving, retained = [], []
            for case_id in data.case_ids:
                case = Store.case(db, case_id)
                if not data.move_completed and self._completed(db, case, stamp, watermark):
                    retained.append(case_id)
                else:
                    moving.append(case_id)
            if not moving:
                raise DomainError("SPLIT_NO_OUTSTANDING_CASES",
                                  "Every selected request is already completed", 409,
                                  {"retained_case_ids": retained})
            sequence = Store.journal(db, "problem_split", "problem", str(problem_id),
                                     {"actor": data.actor, "case_ids": moving,
                                      "retained_completed_case_ids": retained,
                                      "move_completed": data.move_completed,
                                      "title": data.title, "department": data.department},
                                     stamp)
            new_id = self._open_problem(db, problem["grid_id"], problem["category"],
                                        data.department or problem["department"],
                                        data.title or (problem["title"] + " (split)"),
                                        stamp, data.actor, "split")
            for case_id in moving:
                self._detach(db, members[case_id], stamp, sequence)
                self._attach(db, new_id, case_id, stamp, sequence, data.actor, "split")
            self._touch(db, problem_id)
            result = {"source": self._problem_view(db, Store.problem(db, problem_id)),
                      "problem": self._problem_view(db, Store.problem(db, new_id)),
                      "moved_case_ids": moving,
                      "retained_completed_case_ids": retained,
                      "watermark": Store.watermark(db)}
            self._remember_operation(db, data.operation_key, "problem_split",
                                     payload, stamp, result)
            return result

    # ------------------------------------------------------------------ read
    def get(self, problem_id: int, *, as_of: datetime | None = None,
            history: bool = False) -> dict:
        with self.store.transaction() as db:
            watermark = Store.watermark(db)
            problem = Store.problem(db, problem_id)
            stamp = utc(as_of) if as_of is not None else None
            view = self._problem_view(db, problem, watermark, stamp)
            view["watermark"] = watermark
            if history:
                view["history"] = Store.journal_entities(db, "problem", str(problem_id))
            return view

    def cases(self, problem_id: int, *, as_of: datetime | None = None) -> dict:
        with self.store.transaction() as db:
            watermark = Store.watermark(db)
            Store.problem(db, problem_id)
            stamp = utc(as_of) if as_of is not None else None
            members = Store.problem_members(db, problem_id, watermark, stamp)
            items = []
            for member in members:
                item = self._case_view(db, Store.case(db, member["case_id"]), watermark)
                # Report the membership interval that belongs to this problem
                # at the requested time, not the case's grouping today.
                item["problem_id"] = problem_id
                item["joined_at"] = member["joined_at"]
                items.append(item)
            return {"problem_id": problem_id, "as_of": stamp, "watermark": watermark,
                    "items": items}

    def history(self, problem_id: int) -> dict:
        with self.store.transaction() as db:
            Store.problem(db, problem_id)
            entries = Store.journal_entities(db, "problem", str(problem_id))
            members = [dict(item) for item in db.execute(
                "SELECT * FROM problem_members WHERE problem_id=? ORDER BY joined_sequence,"
                " member_id", (problem_id,))]
            merges = [dict(item) for item in db.execute(
                "SELECT * FROM problem_merges WHERE survivor_problem_id=? OR "
                "merged_problem_id=? ORDER BY merge_sequence, merge_id",
                (problem_id, problem_id))]
            return {"problem_id": problem_id, "watermark": Store.watermark(db),
                    "events": entries, "memberships": members, "merges": merges}

    def list(self, status: str | None = None, after: int = 0, limit: int = 50) -> dict:
        if after < 0 or not 1 <= limit <= 200:
            raise DomainError("BAD_PAGE", "Invalid problem page")
        with self.store.transaction() as db:
            watermark = Store.watermark(db)
            merge_state = Store.merge_state(db, watermark)
            clauses, params = ["problem_id>?"], [after]
            if status == "active":
                clauses.append("problem_id NOT IN (SELECT merged_problem_id "
                               "FROM problem_merges WHERE reversed_sequence IS NULL)")
            elif status == "merged":
                clauses.append("problem_id IN (SELECT merged_problem_id "
                               "FROM problem_merges WHERE reversed_sequence IS NULL)")
            elif status is not None:
                raise DomainError("BAD_STATUS", "Unknown problem filter")
            params.append(limit)
            rows = [Store.problem(db, int(item[0])) for item in db.execute(
                "SELECT problem_id FROM problems WHERE " + " AND ".join(clauses) +
                " ORDER BY problem_id LIMIT ?", params)]
            items = [self._problem_view(db, item, watermark) for item in rows]
            for item in items:
                item["merged_into"] = merge_state.get(item["problem_id"])
                item["merged"] = item["merged_into"] is not None
            return {"items": items, "watermark": watermark,
                    "next_cursor": items[-1]["problem_id"] if items else None}
