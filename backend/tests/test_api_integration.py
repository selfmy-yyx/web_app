import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.model import PublicModelError
from integration_model import OfflineModel


BASE = "/api/v1/discussions"


def until(client, room_id, status):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        room = client.get(f"{BASE}/{room_id}").json()
        if room["status"] == status:
            return room
        sleep(0.01)
    raise AssertionError(f"{room_id} did not reach {status}: {room}")


def create_room(client, topic):
    created = client.post(BASE, json={"topic": topic})
    assert created.status_code == 202
    assert created.json()["status"] == "generating"
    assert created.headers["location"] == f"{BASE}/{created.json()['id']}"
    return until(client, created.json()["id"], "awaiting_confirmation")


def parse_stream(text):
    return [
        (int(lines[0][4:]), lines[1][7:], json.loads(lines[2][6:]))
        for block in text.split("\n\n") if block.startswith("id: ")
        for lines in [block.splitlines()]
    ]


def test_parallel_topics_replay_and_duplicate_confirm(tmp_path):
    model = OfflineModel(delay=0.025)
    db_path = tmp_path / "panels.sqlite3"
    with TestClient(create_app(db_path, model=model, turn_pause_seconds=0)) as client:
        a, b = (create_room(client, topic) for topic in ("远程办公", "城市交通"))
        confirm_url = f"{BASE}/{a['id']}/confirm"
        body = {"roster_version": 1}
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post(confirm_url, json=body), range(2)))
        assert all(response.status_code == 200 for response in responses)
        assert responses[0].json() == responses[1].json()
        assert client.post(f"{BASE}/{b['id']}/confirm", json=body).status_code == 200

        live = [client.get(f"{BASE}/{room['id']}").json() for room in (a, b)]
        assert all(room["status"] == "running" for room in live)
        assert {card["id"] for card in client.get(BASE).json()["items"]} == {a["id"], b["id"]}

        for original, snapshot in zip((a, b), live):
            room_id = original["id"]
            final = until(client, room_id, "finished")
            topic = final["topic"]
            assert final["participants"][0]["name"] == f"{topic}嘉宾0"
            assert len(final["utterances"]) == len({item["id"] for item in final["utterances"]})
            assert all(topic in row["text"] for row in final["utterances"])
            assert all(topic in row["statement"] for row in final["findings"])
            assert topic in final["summary"]
            assert final["findings"][0]["kind"] == "disagreement"

            # Last-Event-ID takes precedence over URL after, including across finalization.
            cursor = snapshot["event_cursor"]
            response = client.get(f"{BASE}/{room_id}/events?after=0", headers={"Last-Event-ID": str(cursor)})
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            frames = parse_stream(response.text)
            assert [seq for seq, _, _ in frames] == list(range(cursor + 1, final["event_cursor"] + 1))
            assert all(data["discussion_id"] == room_id for _, _, data in frames)
            assert any(kind == "findings.updated" and any(item["kind"] == "consensus" for item in data["findings"])
                       for _, kind, data in frames)
            assert frames[-1][1] == "discussion.finished"
            assert len([row for row in final["utterances"] if row["kind"] == "opening"]) == 1

        assert model.openings == {"远程办公": 1, "城市交通": 1}
        assert client.post(confirm_url, json=body).json()["status"] == "finished"
        assert client.get(BASE).json() == {"items": []}
        assert client.get(f"{BASE}/{a['id']}/events?after=10000").status_code == 409

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    with TestClient(create_app(db_path, model=model, turn_pause_seconds=0)) as client:
        assert client.get(f"{BASE}/{a['id']}").json()["status"] == "finished"
        assert model.openings == {"远程办公": 1, "城市交通": 1}


@pytest.mark.parametrize("public_message", [None, "模型服务限流（HTTP 429），请稍后重试。"])
def test_failed_model_isolated_from_concurrent_discussion(tmp_path, public_message):
    offline = OfflineModel(delay=0.02)

    def model(operation, **context):
        if public_message and operation == "intentions" and context["topic"] == "失败场":
            raise PublicModelError(public_message)
        return offline(operation, **context)
    with TestClient(create_app(tmp_path / "panels.sqlite3", model=model, turn_pause_seconds=0)) as client:
        failed = create_room(client, "失败场")
        good = create_room(client, "正常场")
        for room in (failed, good):
            assert client.post(f"{BASE}/{room['id']}/confirm", json={"roster_version": 1}).status_code == 200
        failed = until(client, failed["id"], "failed")
        good = until(client, good["id"], "finished")
        expected = {
            "stage": "discussion", "code": "MODEL_STEP_FAILED", "operation": "intentions",
        }
        if public_message:
            expected["message"] = public_message
        assert failed["failure"] == expected
        assert len(failed["utterances"]) == 1
        assert failed["summary"] is None
        frames = parse_stream(client.get(f"{BASE}/{failed['id']}/events").text)
        assert frames[-1][2]["failure"] == failed["failure"]
        assert "offline-private-provider-error" not in repr(frames)
        assert "正常场" in good["summary"]
