import json

from backend.engine import generate_discussion_roster
from backend.store import DiscussionStore


def test_snapshot_and_cursor_stay_consistent_during_roster_commit(tmp_path, roster):
    store = DiscussionStore(tmp_path / "replay.sqlite3")
    store.initialize()
    room_id = store.create("Replay test", 4)["id"]
    before = store.detail(room_id)
    after, events = generate_discussion_roster(
        before, model=lambda operation, **context: {"participants": roster}
    )

    with store.transaction() as reader:
        # Pin the reader's view before a separate connection commits the roster.
        assert reader.execute(
            "SELECT event_cursor FROM discussions WHERE id=?", (room_id,)
        ).fetchone()["event_cursor"] == 0

        with store.transaction(write=True) as writer:
            assert store.persist(writer, before, after, events)

        snapshot = store.snapshot(reader, room_id)
        assert snapshot["status"] == "generating"
        assert snapshot["event_cursor"] == 0
        assert snapshot["participants"] == []

    replay, ended, cursor = store.events_after(room_id, snapshot["event_cursor"])
    assert not ended
    assert cursor == 1
    assert [(row["seq"], row["type"]) for row in replay] == [(1, "discussion.status")]
    payload = json.loads(replay[0]["payload_json"])
    assert payload["status"] == "awaiting_confirmation"
    assert payload["participants"] == store.detail(room_id)["participants"]
    assert len(payload["participants"]) == 5
    assert store.events_after(room_id, cursor)[0] == []
