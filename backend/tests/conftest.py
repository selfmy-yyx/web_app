from collections import deque
from copy import deepcopy
from importlib import import_module
from uuid import NAMESPACE_URL, uuid5

import pytest


TOPIC = "混合办公是否应固定到岗日？"
TIME = "2026-11-18T09:00:00.000Z"
PRIVATE_MARKER = "PRIVATE_REASONING_DO_NOT_PUBLISH"


def fixture_id(label):
    return str(uuid5(NAMESPACE_URL, f"panel-studio-test/{label}"))


class ScriptedModel:
    """A strict queue of model responses, with no network or clocks."""

    def __init__(self, **responses):
        self.responses = {name: deque(items) for name, items in responses.items()}
        self.calls = []

    def __call__(self, operation, **context):
        self.calls.append((operation, deepcopy(context)))
        if operation not in self.responses or not self.responses[operation]:
            pytest.fail(f"Unexpected model call or retry: {operation}")
        return deepcopy(self.responses[operation].popleft())

    def inputs(self, operation):
        return [context for name, context in self.calls if name == operation]


@pytest.fixture
def model():
    return ScriptedModel


@pytest.fixture
def require_behavior():
    """Resolve production behaviors at test time so absent code gives separate REDs."""
    def resolve(name, behavior):
        try:
            module = import_module("backend.engine")
        except ModuleNotFoundError as error:
            if error.name not in {"backend", "backend.engine"}:
                raise
            module = None
        entry_point = getattr(module, name, None)
        if not callable(entry_point):
            pytest.fail(f"{behavior}: missing backend.engine.{name}", pytrace=False)
        return entry_point

    return resolve


@pytest.fixture
def roster():
    rows = [
        ("host", "林遥", "议题主持人", "中立", "#AB7733"),
        ("expert", "陈宁", "组织研究员", "支持小规模试点", "#278776"),
        ("expert", "周凯", "运营顾问", "希望统一到岗", "#5473AA"),
        ("expert", "许岚", "风险顾问", "希望团队自行决定", "#BF6455"),
        ("expert", "沈悦", "产品研究员", "关注参与者反馈", "#8465A3"),
    ]
    return [
        {"role": role, "name": name, "title": title, "stance": stance, "color": color}
        for role, name, title, stance, color in rows
    ]


@pytest.fixture
def running(roster):
    return {
        "id": fixture_id("room"), "topic": TOPIC, "expert_count": 4,
        "status": "running", "roster_version": 1, "created_at": TIME,
        "started_at": TIME, "ended_at": None, "failure": None, "notice": None,
        "event_cursor": 0, "utterances": [], "findings": [], "summary": None,
        "participants": [
            {**person, "id": fixture_id(str(index)), "position": index,
             "public_status": "idle" if index else None, "public_focus": None}
            for index, person in enumerate(roster)
        ],
    }


@pytest.fixture
def utterance(running):
    def make(index, ordinal, text, kind=None):
        return {
            "id": fixture_id(f"utterance-{ordinal}"),
            "participant_id": running["participants"][index]["id"],
            "ordinal": ordinal,
            "kind": kind or ("expert" if index else "opening"),
            "text": text,
            "created_at": TIME,
        }
    return make
