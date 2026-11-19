"""Deterministic, topic-aware model for offline API integration tests."""

from collections import Counter
from time import sleep

from backend.model import ModelError


class OfflineModel:
    def __init__(self, delay=0):
        self.delay = delay
        self.openings = Counter()

    def __call__(self, operation, **context):
        topic = context["topic"]
        if self.delay:
            sleep(self.delay)
        if operation == "roster":
            return {"participants": [
                {"role": "expert" if position else "host", "name": f"{topic}嘉宾{position}",
                 "title": "议题专家" if position else "圆桌主持人", "stance": "关注本场试点",
                 "color": f"#{position + 1:06x}"}
                for position in range(context["expert_count"] + 1)
            ]}
        if operation == "intentions" and topic.startswith("失败"):
            raise ModelError("offline-private-provider-error")
        transcript = context["transcript"]
        expert_turns = [item for item in transcript if item["kind"] == "expert"]
        if operation == "intentions":
            if len(expert_turns) == 3:
                return {"candidates": []}
            expert = context["experts"][len(expert_turns)]
            return {"candidates": [{
                "participant_id": expert["id"], "intent": "supplement",
                "target_utterance_id": transcript[-1]["id"],
                "relevance": 3, "response": 3, "novelty": 3,
            }]}
        if operation == "utterance":
            kind = context["turn"]["kind"]
            if kind == "opening":
                self.openings[topic] += 1
            text = f"{topic}建议小规模试点" if len(expert_turns) < 2 else f"{topic}建议直接实施"
            if kind != "expert":
                text = f"{topic}主持人发言"
            return {"text": text + "。", "public_focus": f"{topic}试点安排"}
        if operation == "findings":
            evidence = [
                {"utterance_id": item["id"], "stance": "support" if index < 2 else "oppose",
                 "position_text": item["text"]}
                for index, item in enumerate(expert_turns)
            ]
            return {"candidates": [{"statement": f"{topic}应该先试点", "evidence": evidence}]}
        if operation == "summary":
            return {"summary": f"{topic}的实施方式存在分歧，仍待确定。"}
        raise AssertionError(operation)
