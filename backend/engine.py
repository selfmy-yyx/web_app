"""Discussion rules and public event projection, independent of HTTP and storage."""

import logging
import re
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from .model import Model, ModelError, ModelTimeout, PublicModelError

log = logging.getLogger(__name__)


def _now():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _object(value):
    if not isinstance(value, dict):
        raise PublicModelError("模型输出必须是对象。")
    return value


def _items(value, field):
    items = _object(value).get(field)
    if not isinstance(items, list):
        raise PublicModelError(f"模型输出缺少有效的 {field} 列表。")
    return items


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise PublicModelError(f"缺少有效的 {field}。")
    return value.strip()


def generate_roster(topic, expert_count=4, *, model: Model):
    topic = topic.strip()
    if not 1 <= len(topic) <= 200 or type(expert_count) is not int or not 2 <= expert_count <= 6:
        raise ValueError("请输入 1–200 个字符的话题和 2–6 位专家。")
    raw = _items(model("roster", topic=topic, expert_count=expert_count), "participants")
    if len(raw) != expert_count + 1:
        raise PublicModelError(f"阵容必须包含 1 位主持人和 {expert_count} 位专家。")
    fields = ("role", "name", "title", "stance", "color")
    people = [{field: _text(_object(person).get(field), field) for field in fields} for person in raw]
    if Counter(person["role"] for person in people) != {"host": 1, "expert": expert_count}:
        raise PublicModelError(f"阵容角色必须为 1 位主持人和 {expert_count} 位专家。")
    colors = {person["color"].casefold() for person in people}
    if len(colors) != len(people) or any(not re.fullmatch(r"#[0-9a-f]{6}", c) for c in colors):
        raise PublicModelError("每位嘉宾必须使用不同的 #RRGGBB 颜色。")
    people.sort(key=lambda person: person["role"] != "host")
    return [
        {**person, "id": str(uuid4()), "position": position,
         "public_status": "idle" if position else None, "public_focus": None}
        for position, person in enumerate(people)
    ]


def _emit(state, events, kind, **data):
    state["event_cursor"] += 1
    events.append({
        "id": state["event_cursor"], "type": kind,
        "data": {"discussion_id": state["id"], "occurred_at": _now(), **deepcopy(data)},
    })


def _status(state, events):
    fields = ("status", "roster_version", "participants", "started_at", "ended_at", "failure")
    _emit(state, events, "discussion.status", **{field: state[field] for field in fields})


def _public_failure(stage, code, error, operation):
    failure = {"stage": stage, "code": code, "operation": operation}
    if isinstance(error, PublicModelError):
        failure["message"] = str(error)
    elif isinstance(error, ModelTimeout):
        failure["message"] = "等待模型服务响应超时。"
    return failure


def generate_discussion_roster(snapshot, *, model: Model):
    """Apply one generation attempt. The caller commits only the current roster version."""
    if snapshot["status"] != "generating":
        raise ValueError("当前场次未处于生成阵容状态。")
    state, events = deepcopy(snapshot), []
    try:
        state["participants"] = generate_roster(state["topic"], state["expert_count"], model=model)
    except ModelError as error:
        code = "ROSTER_TIMEOUT" if isinstance(error, ModelTimeout) else "ROSTER_GENERATION_FAILED"
        state.update(status="failed", participants=[], ended_at=_now(),
                     failure=_public_failure("roster", code, error, "roster"))
    else:
        state.update(status="awaiting_confirmation", ended_at=None, failure=None)
    _status(state, events)
    return state, events


def request_roster_regeneration(snapshot, roster_version):
    if snapshot["roster_version"] != roster_version:
        raise ValueError("阵容已更新，请刷新后重试。")
    failed_roster = (snapshot["failure"] or {}).get("stage") == "roster"
    if snapshot["status"] != "awaiting_confirmation" and not failed_roster:
        raise ValueError("当前无法重新生成阵容。")
    state, events = deepcopy(snapshot), []
    state.update(status="generating", roster_version=roster_version + 1,
                 participants=[], ended_at=None, failure=None)
    _status(state, events)
    return state, events


_INTENTS = {"raise_hand", "quick_reply", "supplement", "rebuttal"}


def _host(state):
    return next(person for person in state["participants"] if person["role"] == "host")


def _retry(operation, on_retry=None, *, model_operation):
    try:
        return operation()
    except ModelError as error:
        log.warning(
            "Model operation %s failed on attempt 1/2: %s", model_operation, error
        )
        if on_retry:
            on_retry(error)
    try:
        return operation()
    except ModelError as error:
        error.operation = model_operation
        log.error(
            "Model operation %s failed on attempt 2/2: %s", model_operation, error
        )
        raise


def select_next(snapshot, *, model: Model, consecutive_no_candidate=0, on_retry=None):
    if snapshot["status"] != "running":
        raise ValueError("只有进行中的场次可以调度发言。")
    host = _host(snapshot)
    transcript = snapshot["utterances"]
    host_turn = {"participant_id": host["id"]}
    if not transcript:
        return {**host_turn, "kind": "opening"}
    experts = {person["id"]: person for person in snapshot["participants"] if person["role"] == "expert"}
    counts = Counter(item["participant_id"] for item in transcript if item["kind"] == "expert")
    total = counts.total()
    if total >= 8 or consecutive_no_candidate >= 2:
        return {**host_turn, "kind": "closing"}
    if total and total % 3 == 0 and transcript[-1]["kind"] == "expert":
        return {**host_turn, "kind": "transition"}

    raw = _retry(lambda: _items(model(
        "intentions", topic=snapshot["topic"], transcript=transcript, experts=list(experts.values()),
    ), "candidates"), on_retry, model_operation="intentions")
    targets = {item["id"] for item in transcript}
    valid = []
    for candidate in raw:
        if not isinstance(candidate, dict):
            continue
        speaker, intent = candidate.get("participant_id"), candidate.get("intent")
        target = candidate.get("target_utterance_id")
        ratings = [candidate.get(field) for field in ("relevance", "response", "novelty")]
        if not isinstance(speaker, str) or speaker not in experts:
            continue
        if not isinstance(intent, str) or intent not in _INTENTS:
            continue
        if target is not None and (not isinstance(target, str) or target not in targets):
            continue
        if any(type(score) is not int or not 0 <= score <= 3 for score in ratings) or not ratings[0]:
            continue
        turn = {"participant_id": speaker, "kind": "expert", "intent": intent,
                "target_utterance_id": target}
        rank = (sum(ratings), -counts[speaker], -experts[speaker]["position"])
        valid.append((rank, turn))
    if valid:
        return max(valid, key=lambda item: item[0])[1]
    return {**host_turn, "kind": "closing" if consecutive_no_candidate else "follow_up",
            "no_candidate": True}


_SENTENCE_END = re.compile(r"[。！？!?]+|(?<![0-9])\.+|\.+(?![0-9])")
_UTTERANCE_FIELDS = ("id", "participant_id", "ordinal", "kind", "text", "created_at")


def _speech(value, field, *, require_ending=True):
    text = _text(value, field)
    sentences = [part for part in _SENTENCE_END.split(text) if any(c.isalnum() for c in part)]
    body = text.rstrip("”’」』）)\"'")
    has_ending = not require_ending or body.endswith(("。", "！", "？", "!", "?", "."))
    if not 1 <= len(sentences) <= 2 or not has_ending or text.startswith(("{", "[", "```")):
        raise PublicModelError(f"{field} 必须是 1–2 句可公开的自然语言。")
    return text


def _extract_findings(state, model):
    transcript = state["utterances"]
    experts = {person["id"] for person in state["participants"] if person["role"] == "expert"}
    by_id = {
        item["id"]: item for item in transcript
        if item["kind"] == "expert" and item["participant_id"] in experts
    }
    raw = _items(model("findings", topic=state["topic"], transcript=transcript), "candidates")
    grouped = {}
    for proposed in raw:
        proposal = _object(proposed)
        statement = _text(proposal.get("statement"), "statement")
        key = re.sub(r"[\W_]+", "", statement).casefold()
        if not key:
            raise PublicModelError("议题缺少有效陈述。")
        canonical, evidence = grouped.setdefault(key, (statement, {}))
        for source in _items(proposal, "evidence"):
            entry = _object(source)
            uid = _text(entry.get("utterance_id"), "utterance_id")
            position = _text(entry.get("position_text"), "position_text")
            stance = entry.get("stance")
            if uid not in by_id:
                raise PublicModelError("模型生成的议题证据没有引用本场已记录的专家发言。")
            if stance not in ("support", "oppose"):
                raise PublicModelError("模型生成的议题证据立场必须是 support 或 oppose。")
            speaker = by_id[uid]["participant_id"]
            previous = evidence.get(speaker)
            if previous is None or by_id[uid]["ordinal"] > by_id[previous[0]]["ordinal"]:
                evidence[speaker] = (uid, stance, position)
            elif previous[0] == uid and previous[1] != stance:
                raise PublicModelError("同一条证据不能同时支持和反对同一议题。")

    findings = []
    for key, (statement, evidence) in grouped.items():
        if len(evidence) < 2:
            continue
        stances = {entry[1] for entry in evidence.values()}
        if stances == {"oppose"}:
            continue
        kind = "disagreement" if "oppose" in stances else "consensus"
        findings.append({
            "id": str(uuid5(NAMESPACE_URL, f"{state['id']}/findings/{key}")),
            "kind": kind,
            "statement": statement,
            "evidence": [
                {"utterance_id": uid, "position_text": position}
                for uid, _, position in sorted(evidence.values(), key=lambda entry: by_id[entry[0]]["ordinal"])
            ],
        })
    return sorted(findings, key=lambda item: (item["kind"], item["id"]))


def _refresh_findings(state, events, model):
    if not any(item["kind"] == "expert" for item in state["utterances"]):
        return
    try:
        findings = _retry(lambda: _extract_findings(state, model), model_operation="findings")
    except ModelError:
        # Preserve the last validated findings and let the discussion continue.
        return
    if findings != state["findings"]:
        state["findings"] = findings
        _emit(state, events, "findings.updated", findings=findings)


def _participant(state, participant_id, kind):
    role = "expert" if kind == "expert" else "host"
    for person in state["participants"]:
        if person["id"] == participant_id and person["role"] == role:
            return person
    raise ValueError("发言者必须属于本场且角色与发言类型匹配。")


def _record(state, events, utterance):
    if state["status"] != "running":
        raise ValueError("只有进行中的场次可以发布发言。")
    item = {field: utterance[field] for field in _UTTERANCE_FIELDS}
    kind = item["kind"]
    if kind not in ("opening", "follow_up", "transition", "expert", "closing"):
        raise ValueError("无效的正式发言类型。")
    _participant(state, item["participant_id"], kind)
    item["text"] = _speech(item["text"], "text")
    transcript = state["utterances"]
    if item["ordinal"] != len(transcript) + 1 or any(row["id"] == item["id"] for row in transcript):
        raise ValueError("发言 ID 必须唯一且序号连续。")
    if kind in ("opening", "closing") and any(row["kind"] == kind for row in transcript):
        raise ValueError("每场只允许一次开场和一次收尾。")
    transcript.append(item)
    _emit(state, events, "utterance.created", utterance=item)


def append_utterance(snapshot, utterance, *, model: Model, on_retry=None):
    state, events = deepcopy(snapshot), []
    _record(state, events, utterance)
    _refresh_findings(state, events, model)
    return state, events


def _compose(state, turn, model, on_retry):
    speaker = _participant(state, turn["participant_id"], turn["kind"])
    private_turn = {key: turn[key] for key in ("kind", "intent", "target_utterance_id") if key in turn}

    def ask():
        result = _object(model(
            "utterance", topic=state["topic"], transcript=state["utterances"],
            speaker=speaker, turn=private_turn,
        ))
        text = _speech(result.get("text"), "text")
        focus = result.get("public_focus")
        return text, _speech(focus, "public_focus", require_ending=False) if focus is not None else None

    text, focus = _retry(ask, on_retry, model_operation="utterance")
    return {
        "id": str(uuid4()), "participant_id": speaker["id"], "kind": turn["kind"],
        "ordinal": len(state["utterances"]) + 1, "text": text, "created_at": _now(),
    }, focus


def _expert_status(state, events, person, status, focus):
    person.update(public_status=status, public_focus=focus)
    _emit(state, events, "participant.status", participant_id=person["id"],
          public_status=status, public_focus=focus)


def publish_turn(snapshot, turn, *, model: Model, on_retry=None):
    if snapshot["status"] != "running" or turn["kind"] == "closing":
        raise ValueError("主持人收尾须由进行中的场次结束流程发布。")
    state, events = deepcopy(snapshot), []
    utterance, focus = _compose(state, turn, model, on_retry)
    speaker = _participant(state, turn["participant_id"], turn["kind"])
    if speaker["role"] == "expert":
        _expert_status(state, events, speaker, "preparing", focus)
        _expert_status(state, events, speaker, "speaking", focus)
    _record(state, events, utterance)
    if speaker["role"] == "expert":
        _expert_status(state, events, speaker, "idle", focus)
        _refresh_findings(state, events, model)
    return state, events


def finish_discussion(snapshot, *, model: Model, on_retry=None):
    if snapshot["status"] != "running":
        raise ValueError("只有进行中的场次可以收尾。")
    state, events = deepcopy(snapshot), []
    host = _host(state)
    _refresh_findings(state, events, model)
    closing, _ = _compose(state, {"participant_id": host["id"], "kind": "closing"}, model, on_retry)
    _record(state, events, closing)

    def summarize():
        result = _object(model(
            "summary", topic=state["topic"], host=host, transcript=state["utterances"],
            findings=state["findings"],
        ))
        text = _text(result.get("summary"), "summary")
        for paragraph in text.splitlines():
            if paragraph.strip():
                _speech(paragraph, "summary")
        return text

    summary = _retry(summarize, on_retry, model_operation="summary")
    state.update(summary=summary, status="finished", ended_at=_now(), notice=None)
    _emit(state, events, "summary.created", summary=summary)
    _emit(state, events, "discussion.finished", status="finished", ended_at=state["ended_at"])
    return state, events


def confirm_discussion(snapshot, roster_version):
    """Return the opened snapshot and events for a conditional commit by the caller."""
    if snapshot["started_at"] is not None:
        return deepcopy(snapshot), []
    if snapshot["status"] != "awaiting_confirmation":
        raise ValueError("阵容尚未就绪。")
    if snapshot["roster_version"] != roster_version:
        raise ValueError("阵容已更新，请刷新后确认。")
    state, events = deepcopy(snapshot), []
    state.update(status="running", started_at=_now())
    _status(state, events)
    return state, events


def advance_discussion(snapshot, *, model: Model):
    """Run one turn. Return its public events and the next snapshot as a batch."""
    if snapshot["status"] != "running":
        raise ValueError("只有进行中的场次可以推进。")
    notices = []
    notice_state = deepcopy(snapshot)

    def record_retry(error):
        if isinstance(error, ModelTimeout) and not notices:
            _emit(notice_state, notices, "discussion.notice", code="MODEL_RETRYING")

    try:
        transcript = snapshot["utterances"]
        previous_miss = bool(transcript and transcript[-1]["kind"] == "follow_up")
        turn = select_next(snapshot, model=model, consecutive_no_candidate=previous_miss, on_retry=record_retry)
        if turn["kind"] == "closing":
            state, events = finish_discussion(snapshot, model=model, on_retry=record_retry)
        else:
            state, events = publish_turn(snapshot, turn, model=model, on_retry=record_retry)
    except ModelError as error:
        state, events = deepcopy(snapshot), []
        code = "MODEL_TIMEOUT" if isinstance(error, ModelTimeout) else "MODEL_STEP_FAILED"
        for person in state["participants"]:
            if person["role"] == "expert":
                person["public_status"] = "idle"
        state.update(status="failed", ended_at=_now(), notice=None,
                     failure=_public_failure("discussion", code, error, getattr(error, "operation", None)))
        _status(state, events)
    for event in events:
        event["id"] += len(notices)
    state["event_cursor"] += len(notices)
    events = notices + events
    if notices and state["status"] == "running":
        _emit(state, events, "discussion.notice", code="MODEL_RETRY_RESOLVED")
    return state, events
