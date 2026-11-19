"""Regression coverage for model failures and discussion transitions."""

import pytest

from backend.engine import (
    advance_discussion,
    confirm_discussion,
    generate_discussion_roster,
    publish_turn,
    request_roster_regeneration,
    select_next,
)
from backend.model import ModelTimeout


@pytest.mark.parametrize(
    ("reply", "code"),
    [(None, "ROSTER_TIMEOUT"), ({"participants": []}, "ROSTER_GENERATION_FAILED")],
)
def test_roster_failure_can_regenerate_same_draft(running, roster, model, reply, code):
    snapshot = {**running, "status": "generating", "started_at": None, "participants": []}

    def failed_model(operation, **context):
        if reply is None:
            raise ModelTimeout("private provider error")
        return reply

    failed, events = generate_discussion_roster(snapshot, model=failed_model)

    assert failed["failure"]["stage"] == "roster"
    assert failed["failure"]["code"] == code
    assert failed["failure"]["operation"] == "roster"
    assert failed["failure"]["message"]
    assert failed["status"] == "failed"
    assert failed["participants"] == []
    assert events[0]["data"]["failure"] == failed["failure"]
    assert "private provider error" not in repr(events)
    with pytest.raises(ValueError, match="尚未就绪"):
        confirm_discussion(failed, 1)

    pending, _ = request_roster_regeneration(failed, 1)
    ready, _ = generate_discussion_roster(pending, model=model(roster=[{"participants": roster}]))
    assert pending["roster_version"] == 2
    assert ready["id"] == snapshot["id"]
    assert ready["topic"] == snapshot["topic"]
    assert ready["expert_count"] == 4
    assert ready["status"] == "awaiting_confirmation"
    assert len(ready["participants"]) == 5
    with pytest.raises(ValueError, match="阵容已更新"):
        confirm_discussion(ready, 1)
    opened, first = confirm_discussion(ready, 2)
    repeated, second = confirm_discussion(opened, 2)
    assert opened["status"] == "running"
    assert len(first) == 1
    assert repeated == opened and second == []


@pytest.mark.parametrize("recovers", [True, False])
def test_model_timeout_retries_once_and_preserves_prior_transcript(
    recovers, running, utterance, model
):
    previous = utterance(0, 1, "有哪些实施风险？")
    running["utterances"] = [previous]
    fake = model(
        intentions=[{"candidates": []}],
        utterance=[{"text": "如果改为分阶段实施呢？"}],
    )
    attempts = 0

    def flaky(operation, **context):
        nonlocal attempts
        if operation == "intentions":
            attempts += 1
            if attempts == 1 or not recovers:
                raise ModelTimeout("private provider error")
        return fake(operation, **context)

    updated, events = advance_discussion(running, model=flaky)

    assert attempts == 2
    assert updated["utterances"][0] == previous
    assert updated["event_cursor"] == events[-1]["id"]
    assert [event["id"] for event in events] == list(range(1, len(events) + 1))
    assert events[0]["data"]["code"] == "MODEL_RETRYING"
    if recovers:
        assert updated["status"] == "running"
        assert updated["utterances"][1]["kind"] == "follow_up"
        assert any(event["data"].get("code") == "MODEL_RETRY_RESOLVED" for event in events)
    else:
        assert updated["status"] == "failed"
        assert updated["failure"] == {
            "stage": "discussion", "code": "MODEL_TIMEOUT", "operation": "intentions",
            "message": "等待模型服务响应超时。",
        }
        assert updated["utterances"] == [previous]
        assert all(person["public_status"] == "idle" for person in updated["participants"][1:])
        assert events[-1]["type"] == "discussion.status"
    assert "private provider error" not in repr(events)


def test_opening_skips_findings(running, model):
    fake = model(utterance=[{"text": "欢迎大家参加讨论。"}])
    updated, events = advance_discussion(running, model=fake)

    assert updated["status"] == "running"
    assert updated["utterances"][0]["kind"] == "opening"
    assert [event["type"] for event in events] == ["utterance.created"]


def test_findings_timeout_does_not_discard_expert_utterance(running, model):
    fake = model(utterance=[{"text": "我认为应该先做试点。"}])
    attempts = 0

    def flaky(operation, **context):
        nonlocal attempts
        if operation == "findings":
            attempts += 1
            raise ModelTimeout("provider timed out")
        return fake(operation, **context)

    turn = {"participant_id": running["participants"][1]["id"], "kind": "expert"}
    updated, events = publish_turn(running, turn, model=flaky)

    assert attempts == 2
    assert updated["status"] == "running"
    assert updated["failure"] is None
    assert updated["findings"] == running["findings"]
    assert updated["utterances"][0]["text"] == "我认为应该先做试点。"
    assert "utterance.created" in [event["type"] for event in events]
    assert "findings.updated" not in [event["type"] for event in events]


def test_host_opens_and_intervenes_after_third_expert_turn(running, utterance, model):
    fake = model()
    assert select_next(running, model=fake)["kind"] == "opening"
    running["utterances"] = [
        utterance(0, 1, "我们开始吧。"),
        *(utterance(index, index + 1, "我有一点补充。") for index in range(1, 4)),
    ]
    turn = select_next(running, model=fake)
    assert turn == {"participant_id": running["participants"][0]["id"], "kind": "transition"}
    assert fake.calls == []


@pytest.mark.parametrize("intent", ["raise_hand", "quick_reply"])
def test_select_next_accepts_other_publicly_invisible_intent_kinds(running, utterance, model, intent):
    expert = running["participants"][2]
    previous = utterance(0, 1, "有哪些实施风险？")
    running["utterances"] = [previous]
    fake = model(intentions=[{"candidates": [{
        "participant_id": expert["id"], "intent": intent, "target_utterance_id": previous["id"],
        "relevance": 3, "response": 2, "novelty": 2,
    }]}])

    turn = select_next(running, model=fake)

    assert (turn["participant_id"], turn["intent"]) == (expert["id"], intent)
