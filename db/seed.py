"""Import the bundled discussion drafts without contacting a model provider."""

import argparse
import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from backend.engine import _now, _status, generate_roster
from backend.store import DiscussionStore

DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / "backend" / "panel.sqlite3"


def seed(path):
    store = DiscussionStore(path)
    store.initialize()
    samples = json.loads(Path(__file__).with_name("samples.json").read_text(encoding="utf-8"))
    results = []
    with store.transaction(write=True) as db:
        for sample in samples:
            room_id = str(uuid5(NAMESPACE_URL, f"ai-panel-studio/samples/{sample['slug']}"))
            topic = sample["topic"]
            raw = sample["participants"]
            count = len(raw) - 1
            inserted = db.execute(
                """INSERT INTO discussions (id, topic, expert_count, status, created_at)
                   VALUES (?, ?, ?, 'generating', ?) ON CONFLICT(id) DO NOTHING""",
                (room_id, topic, count, _now()),
            ).rowcount == 1
            if inserted:
                before = store.snapshot(db, room_id)
                people = generate_roster(topic, count, model=lambda *_args, **_kwargs: {"participants": raw})
                after = {**before, "status": "awaiting_confirmation", "participants": people}
                events = []
                _status(after, events)
                if not store.persist(db, before, after, events):
                    raise RuntimeError(f"Could not import sample {sample['slug']}")
            results.append((topic, room_id, inserted))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=os.getenv("PANEL_DB_PATH", DEFAULT_DATABASE))
    args = parser.parse_args()
    for topic, room_id, inserted in seed(args.db):
        label = "已导入" if inserted else "已存在"
        print(f"{label} {topic}\n  http://localhost:5173/#/discussions/{room_id}")
