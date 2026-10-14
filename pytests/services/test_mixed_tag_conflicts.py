"""独立标签对的混合裁决、最终状态与事务一致性。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import json

import pytest

from src.services.tag_conflict_service import decide_tag_conflicts, parse_conflict_decisions
from src.services.tag_memory_store import StaleTagConflict, TagCandidate, TagMemoryStore, validate_conflict_pairs


@pytest.fixture
def history(tmp_path):
    store = TagMemoryStore(tmp_path / "memory.db")
    store.initialize()
    store.record_batch(session_id="group", session_name="群", external_id="old", summary="旧事实",
                       message_ids=["old"], tags=[
                           TagCandidate(name=name, summary=name, category_path=("群公共", "偏好"),
                                        source_message_ids=["old"], evidence_quotes={"old": name})
                           for name in ("A", "B")])
    old = {tag["name"]: tag for tag in store.list_current_tags("group")}
    return store, old


def decisions(old, actions):
    return [{"new_index": 0, "old_tag_id": old[name]["id"], "old_revision": old[name]["revision"],
             "decision": action, "reason": f"new compared with {name}"} for name, action in zip(("A", "B"), actions, strict=True)]


def record(store, items, tags=None):
    return store.record_batch(session_id="group", session_name="群", external_id="new", summary="新事实",
                              message_ids=["new"], tags=tags or [TagCandidate(
                                  name="N", summary="新事实", category_path=("群公共", "偏好"),
                                  source_message_ids=["new"], evidence_quotes={"new": "新事实"})], conflict_decisions=items)


@pytest.mark.parametrize("actions,expected", [
    (("replace_old", "keep_old"), ("valid", "valid", "superseded")),
    (("keep_old", "replace_old"), ("valid", "valid", "superseded")),
    (("replace_old", "uncertain"), ("disputed", "disputed", "disputed")),
    (("uncertain", "replace_old"), ("disputed", "disputed", "disputed")),
    (("keep_old", "uncertain"), ("valid", "valid", "superseded")),
    (("uncertain", "keep_old"), ("valid", "valid", "superseded")),
])
def test_mixed_actions_are_pair_specific_and_keep_all_evidence(history, actions, expected):
    store, old = history
    record(store, decisions(old, actions))
    conflicts = store.get_conflicts("group")
    assert len(conflicts) == 2
    new_id = conflicts[0]["new_tag_id"]
    actual = tuple(store.tag_detail("group", value)["status"] for value in (old["A"]["id"], old["B"]["id"], new_id))
    assert actual == expected
    for conflict in conflicts:
        evidence = json.loads(conflict["evidence"])
        assert evidence["old"]["sources"] and evidence["new"]["evidence_quotes"]
        assert evidence["applied_new_status"] == expected[2]


def test_duplicate_pair_rolls_back_new_batch_and_old_statuses(history):
    store, old = history
    items = decisions(old, ("replace_old", "keep_old"))
    with pytest.raises(ValueError, match="重复"):
        record(store, items + [items[0]])
    assert store.get_conflicts("group") == []
    assert len(store.list_current_tags("group")) == 2
    assert all(store.tag_detail("group", tag["id"])["revision"] == tag["revision"] for tag in old.values())


def test_stale_second_revision_rolls_back_first_pair_and_evidence(history):
    store, old = history
    items = decisions(old, ("replace_old", "keep_old"))
    items[1]["old_revision"] += 1
    with pytest.raises(StaleTagConflict):
        record(store, items)
    assert store.get_conflicts("group") == []
    assert len(store.list_current_tags("group")) == 2
    assert all(store.tag_detail("group", tag["id"])["revision"] == 0 for tag in old.values())


def test_multiple_effective_replacements_are_rejected_but_different_pairs_are_not(history):
    store, old = history
    items = [{"new_index": index, "old_tag_id": old["A"]["id"], "old_revision": 0,
              "decision": "replace_old", "reason": "replacement"} for index in (0, 1)]
    with pytest.raises(ValueError, match="多个有效"):
        validate_conflict_pairs(items, 2)
    items[1]["decision"] = "keep_old"
    record(store, items, tags=[TagCandidate(name=name, summary=name, category_path=("群公共", "偏好"),
                                         source_message_ids=["new"]) for name in ("N1", "N2")])
    assert store.tag_detail("group", old["A"]["id"])["status"] == "superseded"
    assert {tag["name"] for tag in store.list_current_tags("group")} == {"N1", "B"}


def test_manual_pair_change_recomputes_related_statuses(history):
    store, old = history
    record(store, decisions(old, ("replace_old", "keep_old")))
    rows = {row["old_tag_id"]: row for row in store.get_conflicts("group")}
    store.resolve_conflict("group", rows[old["B"]["id"]]["id"], "replace_old", "both corrected")
    new_id = rows[old["B"]["id"]]["new_tag_id"]
    assert store.tag_detail("group", new_id)["status"] == "valid"
    assert all(store.tag_detail("group", tag["id"])["status"] == "superseded" for tag in old.values())
    store.resolve_conflict("group", rows[old["A"]["id"]]["id"], "keep_old", "A wins")
    assert store.tag_detail("group", new_id)["status"] == "superseded"
    assert all(store.tag_detail("group", tag["id"])["status"] == "valid" for tag in old.values())


def test_next_batch_invalidates_entire_previous_mixed_conflict_chain(history):
    store, old = history
    record(store, decisions(old, ("replace_old", "keep_old")))
    prior_ids = [row["id"] for row in store.get_conflicts("group")]
    current_a = store.tag_detail("group", old["A"]["id"])
    store.record_batch(session_id="group", session_name="群", external_id="later", summary="更新A",
                       message_ids=["later"], tags=[TagCandidate("newA", "更新A", ("群公共", "偏好"), ["later"])],
                       conflict_decisions=[{"new_index": 0, "old_tag_id": current_a["id"],
                                            "old_revision": current_a["revision"], "decision": "replace_old", "reason": "updated"}])
    assert len(store.get_conflicts("group")) == 1
    for prior_id in prior_ids:
        with pytest.raises(ValueError, match="替代"):
            store.resolve_conflict("group", prior_id, "replace_old", "cannot restore obsolete relation")


@pytest.mark.asyncio
async def test_model_mixed_actions_are_not_rejected(monkeypatch):
    tags = [{"id": name, "name": name, "summary": name, "revision": 0,
             "category_path": ["群公共", "偏好"]} for name in ("A", "B")]
    raw = json.dumps({"decisions": [{"old_tag_id": name, "decision": action, "reason": name}
                                    for name, action in (("A", "replace_old"), ("B", "keep_old"))]})
    client = SimpleNamespace(generate_response=AsyncMock(return_value=SimpleNamespace(
        response=raw, prompt_tokens=1, completion_tokens=1, total_tokens=2)))
    monkeypatch.setattr("src.services.llm_service.LLMServiceClient", lambda **kwargs: client)
    monkeypatch.setattr("src.common.prompt_i18n.load_prompt", lambda name: "{input_json}")
    result = await decide_tag_conflicts("group", [TagCandidate("N", "N", ("群公共", "偏好"))], tags)
    assert [item["decision"] for item in result] == ["replace_old", "keep_old"]
    client.generate_response.assert_awaited_once()
    repeated = json.dumps({"decisions": [json.loads(raw)["decisions"][0]] * 2})
    with pytest.raises(ValueError, match="重复"):
        parse_conflict_decisions(repeated, 0, tags)
