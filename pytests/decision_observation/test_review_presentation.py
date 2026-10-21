"""Annotation-only presentation using synthetic SQLite; no production reads."""

import hashlib
import json
import sqlite3

import pytest

from src.decision_observation.dashboard import read_database
from src.decision_observation.review_presentation import ReviewPrivacy
from test_dashboard import fixture as _fixture

fixture = _fixture


def test_review_context_targets_machine_and_readonly_boundary(fixture):
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (fixture.path, fixture.chat)}
    normal = fixture.reader.context(fixture.oid, fixture.eid)
    review = fixture.reader.context(fixture.oid, fixture.eid, annotation_view=True)
    assert normal["items"][0]["name"] == "匿名测试成员"
    assert review["items"][0]["name"].startswith("群友 ")
    assert review["target_items"][0] == review["items"][0]
    assert review["machine"]["gate_result"] == "pass"
    assert review["machine"]["decision_reason"] == "mock_gate"
    assert review["machine"]["execution_result"] == "platform_confirmed"
    assert "privacy_replacements" not in normal
    for path, digest in before.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_target_preview_independent_of_context_page(fixture):
    result = fixture.reader.context(fixture.oid, fixture.eid, page=2, annotation_view=True)
    assert result["items"] == []
    assert len(result["target_items"]) == 1


def test_other_evaluation_receipts_never_become_this_evaluations_result(fixture):
    with sqlite3.connect(fixture.path) as db:
        db.execute(
            "INSERT INTO evaluations SELECT 'second',opportunity_id,2,evaluated_at,feature_version,feature_snapshot,"
            "effective_threshold,calculated_probability,'reject','silent_frequency',weights_hash,evidence FROM evaluations"
        )
    result = fixture.reader.context(fixture.oid, "second", annotation_view=True)
    assert result["machine"]["gate_result"] == "reject"
    assert result["machine"]["decision_reason"] == "silent_frequency"
    assert result["machine"]["execution_result"] == "unknown"
    assert result["machine"]["planner"]["status"] == "unknown"
    opportunity = fixture.reader.context(fixture.oid, "", annotation_view=True)
    assert opportunity["machine"]["gate_result"] is None
    assert opportunity["machine"]["execution_result"] == "unknown"


@pytest.mark.parametrize("conclusion,expected", [("unknown", "unknown"), ("not_observed", "unknown"),
                                                ("definitive_failure", "definitive_failure")])
def test_missing_confirmation_is_never_a_definite_nonreply(fixture, conclusion, expected):
    with sqlite3.connect(fixture.path) as db:
        db.execute("UPDATE delivery_receipts SET conclusion=?", (conclusion,))
    result = fixture.reader.context(fixture.oid, fixture.eid, annotation_view=True)
    assert result["machine"]["execution_result"] == expected


def test_same_name_different_id_and_missing_identity_are_not_merged(fixture):
    with sqlite3.connect(fixture.chat) as db:
        db.execute("UPDATE mai_messages SET user_nickname='同名',user_cardname='旧名',processed_plain_text='同名与旧名和synthetic-user'")
        db.execute("INSERT INTO mai_messages SELECT session_id,platform,'m2',timestamp,'other-user','同名','另一名片','正文',0,0,NULL FROM mai_messages LIMIT 1")
    with read_database(fixture.chat) as db:
        privacy = ReviewPrivacy(db, "group-a", fixture.oid)
        assert privacy.sender("synthetic-user", "m1") != privacy.sender("other-user", "m2")
        assert privacy.sender(None, "m1") != privacy.sender(None, "m2")
        assert privacy.sender("synthetic-user", "m1") == privacy.sender("synthetic-user", "another")
        assert privacy.text("同名") == "群友（同名，身份不确定）"
        rendered = privacy.text("旧名与synthetic-user")
        assert "旧名" not in rendered and "synthetic-user" not in rendered
    page1 = fixture.reader.context(fixture.oid, fixture.eid, annotation_view=True)
    page2 = fixture.reader.context(fixture.oid, fixture.eid, page=2, annotation_view=True)
    assert page1["privacy_replacements"] == page2["privacy_replacements"]
    assert page1["target_items"] == page2["target_items"]


def test_duplicate_chat_identity_cannot_produce_a_target_body(fixture):
    with sqlite3.connect(fixture.chat) as db:
        db.execute("INSERT INTO mai_messages SELECT * FROM mai_messages LIMIT 1")
    result = fixture.reader.context(fixture.oid, fixture.eid, annotation_view=True)
    assert result["target_items"][0]["status"] == "missing_or_ambiguous"
    assert "text" not in result["target_items"][0]


def test_contradictory_sender_evidence_cannot_produce_a_target_body(fixture):
    with sqlite3.connect(fixture.chat) as db:
        db.execute("UPDATE mai_messages SET user_id='different-sender'")
    result = fixture.reader.context(fixture.oid, fixture.eid, annotation_view=True)
    assert result["target_items"][0]["status"] == "missing_or_ambiguous"
    assert "text" not in result["target_items"][0]


def test_multiple_and_missing_candidates_are_preserved_without_guessing(fixture):
    with sqlite3.connect(fixture.path) as db:
        snapshot = fixture.source["evaluations"][0]["feature_snapshot"]
        snapshot["message_event_ids"].append("missing-message")
        db.execute("UPDATE evaluations SET feature_snapshot=?", (json.dumps(snapshot),))
    result = fixture.reader.context(fixture.oid, fixture.eid, annotation_view=True)
    assert result["expected_associated_count"] == 2
    assert result["unavailable_message_event_ids"] == ["missing-message"]
    assert len(result["target_items"]) == 1


def test_annotation_review_route_auth_scope_and_exact_target(fixture):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.webui.dependencies import require_auth
    from src.webui.routers.decision_annotations import annotation_service, router
    from src.webui.routers.decision_observation import reader
    from src.decision_observation.annotation_models import AnnotationError

    class Service:
        def current(self, opportunity_id, target_type, evaluation_id):
            if evaluation_id != fixture.eid or target_type != "evaluation":
                raise AnnotationError(404, "invalid_target")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[annotation_service] = Service
    app.dependency_overrides[reader] = lambda: fixture.reader
    url = f"/api/webui/decision-observation/annotations/{fixture.oid}/review-context"
    with TestClient(app) as client:
        query = {"target_type": "evaluation", "evaluation_id": fixture.eid}
        assert client.get(url, params=query).status_code == 401
        app.dependency_overrides[require_auth] = lambda: "synthetic-admin"
        assert client.get(url, params=query).status_code == 200
        assert client.get(url, params={**query, "evaluation_id": "wrong"}).status_code == 404
        fixture.reader.authorized = frozenset()
        assert client.get(url, params=query).status_code == 404


def put_execution(fixture, stage, *, tools=(), evaluation_id=None, execution="exec", turn="turn", cycle="1",
                  status=None, reason=None, at=None, event_suffix=""):
    evaluation_id = fixture.eid if evaluation_id is None else evaluation_id
    status = status or {"planner_started": "started", "tool_selection": "observed", "not_started": "not_observed"}[stage]
    reason = reason or {"planner_started": "request_entered", "tool_selection": "planner_response", "not_started": "silent_consumption"}[stage]
    at = at if at is not None else fixture.source["evaluations"][0]["evaluated_at"] + (1 if stage == "planner_started" else 2)
    with sqlite3.connect(fixture.path) as db:
        db.execute(
            "INSERT INTO execution_events(event_id,opportunity_id,evaluation_id,execution_id,occurred_at,stage,status,reason,"
            "logical_turn_id,cycle_id,replay_status,message_event_ids,tool_names,link_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{stage}:{evaluation_id}:{execution}:{cycle}:{event_suffix}", fixture.oid, evaluation_id, execution, at,
             stage, status, reason, turn, cycle, "not_observed", "[]", json.dumps(list(tools)), "gate_dispatch"),
        )


def review_machine(fixture):
    return fixture.reader.context(fixture.oid, fixture.eid, annotation_view=True)["machine"]


@pytest.mark.parametrize("tools,decision", [(("wait",), "wait"), (("reply",), "reply"),
                                           (("search",), "other"), ((), "other"), (("wait", "reply"), "mixed")])
def test_gate_pass_does_not_determine_planner_decision(fixture, tools, decision):
    put_execution(fixture, "planner_started")
    put_execution(fixture, "tool_selection", tools=tools)
    result = review_machine(fixture)
    assert result["gate_result"] == "pass"
    assert result["planner"]["status"] == "observed"
    assert result["planner"]["rounds"][0]["decision"] == decision
    assert result["planner"]["rounds"][0]["selection_event_id"]


def test_explicit_skipped_attempt_is_not_inferred_from_gate_or_missing_records(fixture):
    assert review_machine(fixture)["planner"]["status"] == "unknown"
    put_execution(fixture, "not_started", cycle=None)
    result = review_machine(fixture)["planner"]
    assert result["status"] == "not_started" and result["not_started_attempts"] == 1
    assert result["evidence_status"] == "explicit_not_started"


@pytest.mark.parametrize("reason", ["exception", "queued_batch_not_consumed", "unknown_reason"])
def test_generic_missing_start_is_not_proof_of_nonexecution(fixture, reason):
    put_execution(fixture, "not_started", reason=reason)
    assert review_machine(fixture)["planner"]["status"] == "unknown"


@pytest.mark.parametrize("gate", ["reject", "defer", "deferred"])
def test_gate_record_never_invents_planner_or_send_state(fixture, gate):
    with sqlite3.connect(fixture.path) as db:
        db.execute("UPDATE evaluations SET gate_result=?", (gate,))
        db.execute("DELETE FROM reply_events")
    result = review_machine(fixture)
    assert result["gate_result"] == gate
    assert result["planner"]["status"] == "unknown"
    assert result["execution_result"] == "not_observed"


def test_multiple_planner_cycles_keep_distinct_decisions(fixture):
    for cycle, tools in [("1", ("wait",)), ("2", ("reply",))]:
        put_execution(fixture, "planner_started", cycle=cycle)
        put_execution(fixture, "tool_selection", cycle=cycle, tools=tools)
    planner = review_machine(fixture)["planner"]
    assert planner["status"] == "observed"
    assert [r["decision"] for r in planner["rounds"]] == ["wait", "reply"]


def add_evaluation(fixture):
    with sqlite3.connect(fixture.path) as db:
        db.execute("INSERT INTO evaluations SELECT 'other-eval',opportunity_id,2,evaluated_at,feature_version,feature_snapshot,"
                   "effective_threshold,calculated_probability,'pass','other_gate_reason',weights_hash,evidence FROM evaluations LIMIT 1")


def test_other_evaluation_planner_is_not_current_evaluations_decision(fixture):
    add_evaluation(fixture)
    put_execution(fixture, "planner_started", evaluation_id="other-eval")
    put_execution(fixture, "tool_selection", evaluation_id="other-eval", tools=("reply",))
    assert review_machine(fixture)["planner"]["status"] == "unknown"
    other = fixture.reader.context(fixture.oid, "other-eval", annotation_view=True)["machine"]
    assert other["planner"]["rounds"][0]["decision"] == "reply"
    assert other["decision_reason"] == "other_gate_reason"
    assert other["execution_result"] == "unknown"  # Receipts only link the original evaluation.


def test_shared_cycle_cannot_be_attributed_to_either_evaluation_independently(fixture):
    add_evaluation(fixture)
    for eid in (fixture.eid, "other-eval"):
        put_execution(fixture, "planner_started", evaluation_id=eid)
        put_execution(fixture, "tool_selection", evaluation_id=eid, tools=("reply",))
    planner = review_machine(fixture)["planner"]
    assert planner["status"] == "unknown"
    assert planner["evidence_status"] == "shared_cycle"
    assert planner["rounds"] == []


@pytest.mark.parametrize("problem", ["missing_start", "missing_selection", "missing_turn", "wrong_cycle", "wrong_time", "before_gate", "invalid_tools"])
def test_incomplete_or_contradictory_lineage_stays_unknown(fixture, problem):
    if problem != "missing_start":
        put_execution(fixture, "planner_started", turn=None if problem == "missing_turn" else "turn")
    if problem != "missing_selection":
        put_execution(fixture, "tool_selection", tools=("reply",), cycle="2" if problem == "wrong_cycle" else "1",
                      at=0 if problem == "before_gate" else (fixture.source["evaluations"][0]["evaluated_at"] + 0.5 if problem == "wrong_time" else None))
    if problem == "invalid_tools":
        with sqlite3.connect(fixture.path) as db:
            db.execute("UPDATE execution_events SET tool_names='not-json' WHERE stage='tool_selection'")
    assert review_machine(fixture)["planner"]["status"] == "unknown"


def test_generated_reply_and_failed_send_do_not_change_planner_evidence(fixture):
    put_execution(fixture, "planner_started")
    put_execution(fixture, "tool_selection", tools=("reply",))
    with sqlite3.connect(fixture.path) as db:
        assert db.execute("SELECT 1 FROM reply_events WHERE stage='generation'").fetchone() is not None
        db.execute("UPDATE delivery_receipts SET conclusion='definitive_failure'")
    result = review_machine(fixture)
    assert result["planner"]["rounds"][0]["decision"] == "reply"
    assert result["execution_result"] == "definitive_failure"


def test_driver_attempt_with_missing_receipts_is_unknown(fixture):
    with sqlite3.connect(fixture.path) as db:
        db.execute("DELETE FROM delivery_links")
    assert review_machine(fixture)["execution_result"] == "unknown"


def test_no_driver_observation_is_not_a_definitive_nonreply(fixture):
    with sqlite3.connect(fixture.path) as db:
        db.execute("DELETE FROM reply_events WHERE stage='driver_send'")
    assert review_machine(fixture)["execution_result"] == "not_observed"


def test_old_schema_missing_tables_is_unknown(fixture):
    with sqlite3.connect(fixture.path) as db:
        db.execute("DROP TABLE execution_events")
        db.execute("DROP TABLE reply_events")
    result = review_machine(fixture)
    assert result["planner"]["status"] == "unknown"
    assert result["execution_result"] == "unknown"


def test_conflicting_selections_for_the_same_cycle_are_not_a_final_decision(fixture):
    put_execution(fixture, "planner_started")
    put_execution(fixture, "tool_selection", tools=("reply",))
    put_execution(fixture, "tool_selection", tools=("wait",), event_suffix="conflicting")
    assert review_machine(fixture)["planner"]["status"] == "unknown"


def test_shared_cycle_across_opportunities_cannot_be_attributed_independently(fixture):
    put_execution(fixture, "planner_started")
    put_execution(fixture, "tool_selection", tools=("reply",))
    with sqlite3.connect(fixture.path) as db:
        db.execute("INSERT INTO opportunities SELECT 'other-opportunity',session_id,created_at,run_id,policy_version,"
                   "source_type,finalized_at,status,account_id FROM opportunities LIMIT 1")
        db.execute("INSERT INTO execution_events SELECT event_id||'-shared','other-opportunity',NULL,execution_id,"
                   "occurred_at,stage,status,reason,logical_turn_id,cycle_id,replay_status,message_event_ids,tool_names,"
                   "link_source,wait_tool_call_id,parent_execution_id FROM execution_events WHERE stage='tool_selection'")
    assert review_machine(fixture)["planner"]["evidence_status"] == "shared_cycle"


def test_conflicting_platform_evidence_is_not_confirmed_success_or_failure(fixture):
    with sqlite3.connect(fixture.path) as db:
        # A second contradictory receipt for every previously confirmed attempt.
        db.execute("INSERT INTO delivery_receipts SELECT receipt_id||'-conflict',scope_id,session_id,platform,account_id,"
                   "route_scope,outbound_id,driver_id,driver_kind,driver_status,platform_message_id,delivery_id,acknowledged,retcode,"
                   "observed_at,source,attempt_id,ambiguous,failure_evidence,recorded_at,'definitive_failure',evidence_strength "
                   "FROM delivery_receipts")
        db.execute("INSERT INTO delivery_links SELECT receipt_id||'-conflict',parent_event_id,linked_at FROM delivery_links")
    assert review_machine(fixture)["execution_result"] == "unknown"
