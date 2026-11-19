"""RED contract for the synchronous engine seam, separate from HTTP and SSE I/O.

The engine receives a callable model(operation, **context). Step functions return
(snapshot, events); public events use the API's {id, type, data} envelope.
"""

import json
import re
from collections import Counter
from copy import deepcopy

import pytest

from conftest import PRIVATE_MARKER, TOPIC


def test_generate_roster_returns_one_host_and_four_complete_experts(
    require_behavior, model, roster
):
    fake = model(roster=[{"participants": roster}])
    generate = require_behavior("generate_roster", "Generate a complete 1+4 roster")

    participants = generate(TOPIC, 4, model=fake)

    assert fake.inputs("roster") == [{"topic": TOPIC, "expert_count": 4}]
    assert Counter(item["role"] for item in participants) == {"host": 1, "expert": 4}
    for actual, expected in zip(participants, roster, strict=True):
        for field in ("name", "title", "stance", "color", "role"):
            assert actual[field].strip() == expected[field]
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", actual["color"])
    assert len({item["color"].casefold() for item in participants}) == 5


@pytest.mark.parametrize("defect", ["wrong-count", "missing-title", "no-host"])
def test_generate_roster_rejects_invalid_model_output(require_behavior, model, roster, defect):
    if defect == "wrong-count":
        roster.pop()
    elif defect == "missing-title":
        del roster[2]["title"]
    else:
        roster[0]["role"] = "expert"
    fake = model(roster=[{"participants": roster}])
    generate = require_behavior("generate_roster", f"Reject invalid roster ({defect})")

    with pytest.raises(ValueError):
        generate(TOPIC, 4, model=fake)
    assert len(fake.inputs("roster")) == 1


def candidate(person, intent, evidence, ratings=(3, 3, 3)):
    relevance, response, novelty = ratings
    return {
        "participant_id": person["id"], "intent": intent,
        "target_utterance_id": evidence["id"],
        "relevance": relevance, "response": response, "novelty": novelty,
        "reasoning": PRIVATE_MARKER,
    }


def test_next_speaker_changes_with_current_transcript_and_intents(
    require_behavior, model, running, utterance
):
    _, a, b, *_ = running["participants"]
    a_statement = utterance(1, 1, "每周应保留两个固定到岗日。")
    b_statement = utterance(2, 2, "所有团队的到岗成本都一样。")
    first = {**running, "utterances": [a_statement]}
    second = {**running, "utterances": [a_statement, b_statement]}
    fake = model(intentions=[
        {"candidates": [candidate(a, "supplement", a_statement, (1, 1, 1)),
                        candidate(b, "supplement", a_statement)]},
        {"candidates": [candidate(a, "rebuttal", b_statement),
                        candidate(b, "supplement", b_statement, (1, 1, 1))]},
    ])
    choose = require_behavior("select_next", "Select a supplement then a rebuttal from current context")

    supplement = choose(first, model=fake)
    rebuttal = choose(second, model=fake)

    assert (supplement["participant_id"], supplement["intent"]) == (b["id"], "supplement")
    assert (rebuttal["participant_id"], rebuttal["intent"]) == (a["id"], "rebuttal")
    assert [call["transcript"] for call in fake.inputs("intentions")] == [
        first["utterances"], second["utterances"],
    ]


def test_next_speaker_can_be_the_expert_who_just_spoke(require_behavior, model, running, utterance):
    _, a, b, *_ = running["participants"]
    latest = utterance(1, 1, "我建议先试点。")
    running["utterances"] = [latest]
    fake = model(intentions=[{"candidates": [
        candidate(b, "rebuttal", latest, (1, 1, 1)),
        candidate(a, "supplement", latest),
    ]}])
    choose = require_behavior("select_next", "Allow the same expert to speak consecutively")

    turn = choose(running, model=fake)

    assert (turn["participant_id"], turn["kind"], turn["intent"]) == (a["id"], "expert", "supplement")


@pytest.mark.parametrize("previous_misses,kind", [(0, "follow_up"), (1, "closing")],
                         ids=["first-miss-follow-up", "second-miss-closing"])
def test_no_candidate_selects_host(require_behavior, model, running, utterance, previous_misses, kind):
    running["utterances"] = [utterance(0, 1, "有哪些实施风险？")]
    fake = model(intentions=[{"candidates": []}])
    choose = require_behavior("select_next", f"Select host {kind} after {previous_misses + 1} empty round(s)")

    turn = choose(running, model=fake, consecutive_no_candidate=previous_misses)

    assert (turn["participant_id"], turn["kind"]) == (running["participants"][0]["id"], kind)
    assert len(fake.inputs("intentions")) == 1


def test_eighth_expert_turn_selects_host_closing(require_behavior, model, running, utterance):
    running["utterances"] = [
        utterance(index % 4 + 1, index + 1, "我补充一点。") for index in range(8)
    ]
    fake = model()
    choose = require_behavior("select_next", "Stop expert scheduling at eight expert turns")

    turn = choose(running, model=fake)

    assert (turn["participant_id"], turn["kind"]) == (running["participants"][0]["id"], "closing")
    assert fake.calls == []


@pytest.mark.parametrize("valid_text", ["建议先试点。", "建议先试点。随后安排复盘。"],
                         ids=["one-sentence", "two-sentences"])
def test_invalid_turn_is_retried_and_only_valid_utterance_is_published(
    require_behavior, model, running, valid_text
):
    speaker = running["participants"][1]
    fake = model(
        utterance=[{"text": "第一句。第二句。第三句。"}, {"text": valid_text}],
        findings=[{"candidates": []}],
    )
    publish = require_behavior("publish_turn", "Retry a three-sentence reply and publish only valid speech")

    updated, events = publish(
        running, {"participant_id": speaker["id"], "kind": "expert", "intent": "supplement"},
        model=fake,
    )

    assert len(fake.inputs("utterance")) == 2
    assert [item["text"] for item in updated["utterances"]] == [valid_text]
    assert [event["data"]["utterance"]["text"] for event in events
            if event["type"] == "utterance.created"] == [valid_text]


def test_live_events_expose_only_public_state_and_formal_speech(require_behavior, model, running):
    speaker = running["participants"][1]
    focus = "小规模试点"
    turn = {"participant_id": speaker["id"], "kind": "expert", "intent": "supplement",
            "score": 9, "reasoning": PRIVATE_MARKER}
    fake = model(
        utterance=[{"text": "我建议先试点。", "public_focus": focus, "reasoning": PRIVATE_MARKER}],
        findings=[{"candidates": []}],
    )
    publish = require_behavior("publish_turn", "Publish allowlisted state and speech without private scheduling data")
    assert speaker["public_status"] == "idle"

    updated, events = publish(running, turn, model=fake)

    common = {"discussion_id", "occurred_at"}
    allowed = {
        "participant.status": common | {"participant_id", "public_status", "public_focus"},
        "utterance.created": common | {"utterance"},
        "findings.updated": common | {"findings"},
    }
    for event in events:
        assert set(event["data"]) == allowed[event["type"]]
        assert event["data"]["discussion_id"] == running["id"]
    statuses = [event["data"] for event in events if event["type"] == "participant.status"]
    assert [item["public_status"] for item in statuses] == ["preparing", "speaking", "idle"]
    assert all(item["participant_id"] == speaker["id"] for item in statuses)
    assert statuses[1]["public_focus"] == focus
    formal = [event["data"]["utterance"] for event in events if event["type"] == "utterance.created"]
    assert len(formal) == 1
    assert set(formal[0]) == {"id", "participant_id", "ordinal", "kind", "text", "created_at"}
    assert formal[0]["kind"] == "expert"
    assert updated["utterances"] == formal
    assert updated["participants"][1]["public_status"] == "idle"
    public = json.dumps({"snapshot": updated, "events": events}, ensure_ascii=False)
    for secret in (PRIVATE_MARKER, '"reasoning"', '"intent"', '"score"'):
        assert secret not in public


STATEMENT = "应先开展小规模试点"


def proposed_finding(*positions):
    return {
        "statement": STATEMENT,
        "evidence": [
            {"utterance_id": utterance["id"], "stance": stance, "position_text": utterance["text"]}
            for utterance, stance in positions
        ],
    }


def assert_updated_while_running(updated, events):
    assert updated["status"] == "running"
    assert updated["summary"] is None
    changes = [event["data"]["findings"] for event in events if event["type"] == "findings.updated"]
    assert changes == [updated["findings"]]
    kinds = [event["type"] for event in events]
    assert kinds.index("utterance.created") < kinds.index("findings.updated")
    assert "summary.created" not in kinds


def test_new_expert_statement_publishes_one_consensus_before_summary(
    require_behavior, model, running, utterance
):
    a = utterance(1, 1, "我支持先小规模试点。")
    b = utterance(2, 2, "我也支持先小规模试点。")
    running["utterances"] = [a]
    point = proposed_finding((a, "support"), (b, "support"))
    fake = model(findings=[{"candidates": [point, deepcopy(point)]}])
    append = require_behavior("append_utterance", "Publish live consensus and merge repeated findings")

    updated, events = append(running, b, model=fake)

    assert fake.inputs("findings")[0]["transcript"] == [a, b]
    assert [(item["kind"], item["statement"]) for item in updated["findings"]] == [
        ("consensus", STATEMENT)
    ]
    assert [e["utterance_id"] for e in updated["findings"][0]["evidence"]] == [a["id"], b["id"]]
    assert all(set(e) == {"utterance_id", "position_text"} for e in updated["findings"][0]["evidence"])
    assert_updated_while_running(updated, events)


def test_repetition_by_one_expert_does_not_create_consensus(
    require_behavior, model, running, utterance
):
    a1 = utterance(1, 1, "我支持先试点。")
    a2 = utterance(1, 2, "我仍然支持先试点。")
    running["utterances"] = [a1]
    fake = model(findings=[{"candidates": [proposed_finding((a1, "support"), (a2, "support"))]}])
    append = require_behavior("append_utterance", "Count distinct supporters rather than repeated speeches")

    updated, events = append(running, a2, model=fake)

    assert updated["utterances"] == [a1, a2]
    assert updated["findings"] == []
    assert all(event["data"]["findings"] == [] for event in events if event["type"] == "findings.updated")


def test_opposition_replaces_existing_consensus_with_disagreement(
    require_behavior, model, running, utterance
):
    a = utterance(1, 1, "我支持先试点。")
    b = utterance(2, 2, "我也支持先试点。")
    c = utterance(3, 3, "我反对试点，建议直接实施。")
    running["utterances"] = [a, b]
    old = proposed_finding((a, "support"), (b, "support"))
    running["findings"] = [{"id": "old", "kind": "consensus", "statement": STATEMENT,
                           "evidence": [{"utterance_id": e["utterance_id"], "position_text": e["position_text"]}
                                        for e in old["evidence"]]}]
    opposition = proposed_finding((a, "support"), (b, "support"), (c, "oppose"))
    fake = model(findings=[{"candidates": [opposition]}])
    append = require_behavior("append_utterance", "Replace contested consensus with live disagreement")

    updated, events = append(running, c, model=fake)

    assert [(item["kind"], item["statement"]) for item in updated["findings"]] == [
        ("disagreement", STATEMENT)
    ]
    assert {e["utterance_id"] for e in updated["findings"][0]["evidence"]} == {a["id"], b["id"], c["id"]}
    assert_updated_while_running(updated, events)


def test_host_finishes_with_natural_language_summary_grounded_in_this_discussion(
    require_behavior, model, running, utterance
):
    a = utterance(1, 1, "我们先做试点，由项目组统一制定评价指标。")
    b = utterance(2, 2, "我也支持先做试点，但评价指标应由团队自行设计。")
    running["utterances"] = [a, b]
    fake = model(
        findings=[{"candidates": [proposed_finding((a, "support"), (b, "support")), {
            "statement": "评价指标应由项目组统一制定",
            "evidence": [
                {"utterance_id": a["id"], "stance": "support", "position_text": "项目组统一制定"},
                {"utterance_id": b["id"], "stance": "oppose", "position_text": "团队自行设计"},
            ],
        }]}],
        utterance=[{"text": "现在进入总结。"}],
        summary=[{"summary": "大家同意先开展小规模试点。\n\n评价指标应由项目组统一制定还是由团队自行设计，仍未达成一致。"}],
    )
    finish = require_behavior("finish_discussion", "Generate and publish the host's grounded natural-language summary")

    updated, events = finish(running, model=fake)

    summary = updated["summary"]
    assert isinstance(summary, str)
    assert "先开展小规模试点" in summary and "评价指标" in summary and "未达成一致" in summary
    assert not summary.lstrip().startswith(("{", "["))
    context, = fake.inputs("summary")
    assert context["host"]["id"] == running["participants"][0]["id"]
    assert [item["id"] for item in context["transcript"] if item["kind"] == "expert"] == [a["id"], b["id"]]
    assert {(item["kind"], item["statement"]) for item in context["findings"]} == {
        ("consensus", STATEMENT), ("disagreement", "评价指标应由项目组统一制定"),
    }
    assert updated["status"] == "finished"
    assert updated["ended_at"]
    assert [event["type"] for event in events][-3:] == [
        "utterance.created", "summary.created", "discussion.finished",
    ]
    assert events[-3]["data"]["utterance"]["kind"] == "closing"
    assert events[-3]["data"]["utterance"]["participant_id"] == running["participants"][0]["id"]
    assert events[-2]["data"]["summary"] == summary
    assert events[-1]["data"]["ended_at"] == updated["ended_at"]
