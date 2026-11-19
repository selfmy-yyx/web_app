"""SQLite persistence for discussion snapshots and the public event log."""

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from .engine import _now

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "init.sql"

PERSON = "id role position name title stance color public_status public_focus".split()
SPEECH = "id participant_id ordinal kind text created_at".split()


def _values(record, columns):
    return tuple(record[column] for column in columns)


class DiscussionStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def transaction(self, *, write=False):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            exists = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'discussions'"
            ).fetchone()
            if not exists:
                connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def create(self, topic, expert_count):
        room_id = str(uuid4())
        with self.transaction(write=True) as db:
            db.execute(
                """INSERT INTO discussions (id, topic, expert_count, status, created_at)
                   VALUES (?, ?, ?, 'generating', ?)""", (room_id, topic, expert_count, _now()),
            )
        return {"id": room_id, "status": "generating", "roster_version": 1}

    def running(self):
        with self.transaction() as db:
            return [dict(row) for row in db.execute(
                """SELECT id, topic, expert_count, status, started_at FROM discussions
                   WHERE status = 'running' ORDER BY started_at DESC, id DESC"""
            )]

    def pending(self):
        with self.transaction() as db:
            return [row["id"] for row in db.execute(
                "SELECT id FROM discussions WHERE status IN ('generating', 'running')"
            )]

    def detail(self, room_id):
        with self.transaction() as db:
            return self.snapshot(db, room_id)

    def snapshot(self, db, room_id):
        row = db.execute("SELECT * FROM discussions WHERE id = ?", (room_id,)).fetchone()
        if row is None:
            return None
        snapshot = dict(row)
        stage, code = snapshot.pop("failure_stage"), snapshot.pop("failure_code")
        snapshot["failure"] = {"stage": stage, "code": code} if stage else None
        if stage:
            row = db.execute(
                "SELECT payload_json FROM live_events WHERE discussion_id=? AND seq=? AND type='discussion.status'",
                (room_id, snapshot["event_cursor"]),
            ).fetchone()
            if row:
                detail = json.loads(row["payload_json"]).get("failure") or {}
                snapshot["failure"].update(
                    {field: detail[field] for field in ("operation", "message") if isinstance(detail.get(field), str)}
                )

        def rows(table, fields, order):
            return [dict(row) for row in db.execute(
                f"SELECT {', '.join(fields)} FROM {table} WHERE discussion_id = ? ORDER BY {order}",
                (room_id,),
            )]

        snapshot["participants"] = rows("participants", PERSON, "position")
        snapshot["utterances"] = rows("utterances", SPEECH, "ordinal")
        snapshot["findings"] = rows("findings", ["id", "kind", "statement"], "kind, id")
        evidence = {}
        for item in db.execute(
            """SELECT e.finding_id, e.utterance_id, e.position_text
               FROM finding_evidence AS e JOIN utterances AS u
                    ON u.discussion_id = e.discussion_id AND u.id = e.utterance_id
               WHERE e.discussion_id = ? ORDER BY u.ordinal""", (room_id,),
        ):
            evidence.setdefault(item["finding_id"], []).append(
                {"utterance_id": item["utterance_id"], "position_text": item["position_text"]}
            )
        for finding in snapshot["findings"]:
            finding["evidence"] = evidence.get(finding["id"], [])
        snapshot["notice"] = None
        if snapshot["status"] == "running":
            notice = db.execute(
                """SELECT payload_json FROM live_events WHERE discussion_id = ?
                   AND type = 'discussion.notice' ORDER BY seq DESC LIMIT 1""", (room_id,),
            ).fetchone()
            if notice and json.loads(notice["payload_json"])["code"] == "MODEL_RETRYING":
                snapshot["notice"] = "MODEL_RETRYING"
        return snapshot

    def persist(self, db, before, after, events):
        """Store a conditional transition and its events inside the caller's transaction."""
        room_id = before["id"]
        cursor = before["event_cursor"]
        if after["id"] != room_id or after["event_cursor"] != cursor + len(events):
            raise ValueError("Invalid discussion event batch")
        for seq, event in enumerate(events, cursor + 1):
            if event["id"] != seq or event["data"]["discussion_id"] != room_id:
                raise ValueError("Invalid discussion event sequence")

        failure = after["failure"] or {}
        changed = db.execute(
            """UPDATE discussions SET status=?, roster_version=?, event_cursor=?,
                      started_at=?, ended_at=?, summary=?, failure_stage=?, failure_code=?
               WHERE id=? AND event_cursor=? AND status=? AND roster_version=?""",
            (*_values(after, ("status", "roster_version", "event_cursor", "started_at", "ended_at", "summary")),
             failure.get("stage"), failure.get("code"), room_id, cursor,
             before["status"], before["roster_version"]),
        ).rowcount
        if changed != 1:
            return False

        old_people, new_people = before["participants"], after["participants"]
        if [p["id"] for p in old_people] != [p["id"] for p in new_people]:
            db.execute("DELETE FROM participants WHERE discussion_id = ?", (room_id,))
            db.executemany(
                f"INSERT INTO participants (discussion_id, {', '.join(PERSON)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(room_id, *_values(person, PERSON)) for person in new_people],
            )
        elif old_people != new_people:
            db.executemany(
                "UPDATE participants SET public_status=?, public_focus=? WHERE discussion_id=? AND id=?",
                [(person["public_status"], person["public_focus"], room_id, person["id"])
                 for person in new_people],
            )

        db.executemany(
            f"INSERT INTO utterances (discussion_id, {', '.join(SPEECH)}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(room_id, *_values(item, SPEECH)) for item in after["utterances"][len(before["utterances"]):]],
        )
        if before["findings"] != after["findings"]:
            db.execute("DELETE FROM findings WHERE discussion_id = ?", (room_id,))
            for finding in after["findings"]:
                db.execute(
                    "INSERT INTO findings VALUES (?, ?, ?, ?)",
                    (room_id, *_values(finding, ("id", "kind", "statement"))),
                )
                db.executemany(
                    "INSERT INTO finding_evidence VALUES (?, ?, ?, ?)",
                    [(room_id, finding["id"], *_values(item, ("utterance_id", "position_text")))
                     for item in finding["evidence"]],
                )

        db.executemany(
            "INSERT INTO live_events VALUES (?, ?, ?, ?, ?)",
            [(room_id, event["id"], event["type"], json.dumps(event["data"], ensure_ascii=False),
              event["data"]["occurred_at"]) for event in events],
        )
        return True

    def events_after(self, room_id, cursor):
        with self.transaction() as db:
            room = db.execute(
                "SELECT status, failure_stage, event_cursor FROM discussions WHERE id=?",
                (room_id,),
            ).fetchone()
            events = db.execute(
                """SELECT seq, type, payload_json FROM live_events WHERE discussion_id=?
                   AND seq>? ORDER BY seq LIMIT 64""", (room_id, cursor),
            ).fetchall()
        end = room["status"] == "finished" or room["failure_stage"] == "discussion"
        return events, end, room["event_cursor"]
