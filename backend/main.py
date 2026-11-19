"""FastAPI HTTP and SSE for the single-process local studio."""

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, StringConstraints

from .engine import (
    _now, _status, advance_discussion, confirm_discussion,
    generate_discussion_roster, request_roster_regeneration,
)
from .model import OpenAICompatibleModel
from .store import DiscussionStore

log = logging.getLogger(__name__)
MAX_CURSOR = 2**53 - 1


class CreateDiscussion(BaseModel):
    topic: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    expert_count: Annotated[int, Field(strict=True, ge=2, le=6)] = 4


class RosterVersion(BaseModel):
    roster_version: Annotated[int, Field(strict=True, ge=1, le=MAX_CURSOR)]


class ApiError(Exception):
    def __init__(self, status, code, message, field=None):
        self.status = status
        self.body = {"code": code, "message": message}
        if field:
            self.body["field"] = field


def not_found():
    return ApiError(404, "DISCUSSION_NOT_FOUND", "这场讨论不存在。")


def terminal(snapshot):
    return snapshot["status"] == "finished" or (snapshot["failure"] or {}).get("stage") == "discussion"


def configured_model(operation, **context):
    return OpenAICompatibleModel.from_env()(operation, **context)


class DiscussionTasks:
    def __init__(self, store, model, turn_pause_seconds):
        self.store = store
        self.model = model
        self.turn_pause_seconds = turn_pause_seconds
        self.jobs = {}
        self.subscribers = {}

    def notify(self, room_id):
        for signal in self.subscribers.get(room_id, ()):
            signal.set()

    def start(self, room_id):
        if room_id not in self.jobs:
            self.jobs[room_id] = asyncio.create_task(self.run(room_id))

    async def close(self):
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    async def run(self, room_id):
        try:
            while (before := self.store.detail(room_id)) is not None:
                if before["status"] == "generating":
                    step = generate_discussion_roster
                elif before["status"] == "running":
                    step = advance_discussion
                else:
                    return
                try:
                    after, events = await asyncio.to_thread(step, before, model=self.model)
                except Exception as error:
                    log.error("Discussion %s failed unexpectedly: %s", room_id, type(error).__name__)
                    after, events = self.failed_step(before)
                with self.store.transaction(write=True) as db:
                    committed = self.store.persist(db, before, after, events)
                if committed:
                    self.notify(room_id)
                    if before["status"] == after["status"] == "running" and self.turn_pause_seconds:
                        await asyncio.sleep(self.turn_pause_seconds)
        finally:
            self.jobs.pop(room_id, None)

    @staticmethod
    def failed_step(before):
        stage = "roster" if before["status"] == "generating" else "discussion"
        code = "ROSTER_GENERATION_FAILED" if stage == "roster" else "MODEL_STEP_FAILED"
        participants = [
            {**person, "public_status": "idle"} if person["role"] == "expert" else person
            for person in before["participants"]
        ]
        after = {**before, "status": "failed", "ended_at": _now(), "notice": None,
                 "participants": participants, "failure": {"stage": stage, "code": code}}
        events = []
        _status(after, events)
        return after, events

    async def stream(self, room_id, cursor):
        signal = asyncio.Event()
        room_signals = self.subscribers.setdefault(room_id, set())
        room_signals.add(signal)
        try:
            yield ": connected\n\n"
            while True:
                signal.clear()
                events, ended, latest = self.store.events_after(room_id, cursor)
                for row in events:
                    cursor = row["seq"]
                    yield f"id: {cursor}\nevent: {row['type']}\ndata: {row['payload_json']}\n\n"
                if cursor < latest:
                    continue
                if ended:
                    return
                try:
                    await asyncio.wait_for(signal.wait(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            room_signals.discard(signal)
            if not room_signals:
                self.subscribers.pop(room_id, None)


def create_app(db_path=None, *, model=configured_model, turn_pause_seconds=30):
    path = db_path if db_path is not None else os.getenv("PANEL_DB_PATH", Path(__file__).with_name("panel.sqlite3"))
    store = DiscussionStore(path)
    tasks = DiscussionTasks(store, model, turn_pause_seconds)

    @asynccontextmanager
    async def lifespan(_app):
        store.initialize()
        for room_id in store.pending():
            tasks.start(room_id)
        try:
            yield
        finally:
            await tasks.close()

    app = FastAPI(title="AI Panel Studio", lifespan=lifespan)
    prefix = "/api/v1/discussions"

    @app.exception_handler(ApiError)
    async def api_error(_request, error):
        return JSONResponse({"error": error.body}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, error):
        field = error.errors()[0]["loc"][-1]
        return await api_error(_request, ApiError(422, "VALIDATION_ERROR", "请求参数无效。", str(field)))

    @app.exception_handler(sqlite3.OperationalError)
    async def unavailable(_request, _error):
        return await api_error(_request, ApiError(503, "SERVICE_UNAVAILABLE", "数据库暂时不可用，请重试。"))

    @app.get(prefix)
    async def list_running():
        return {"items": store.running()}

    @app.post(prefix, status_code=202)
    async def create(body: CreateDiscussion):
        accepted = store.create(body.topic, body.expert_count)
        tasks.start(accepted["id"])
        return JSONResponse(accepted, status_code=202, headers={"Location": f"{prefix}/{accepted['id']}"})

    @app.get(prefix + "/{discussion_id}")
    async def detail(discussion_id: UUID):
        snapshot = store.detail(str(discussion_id))
        if snapshot is None:
            raise not_found()
        return snapshot

    @app.post(prefix + "/{discussion_id}/roster/regenerate", status_code=202)
    async def regenerate(discussion_id: UUID, body: RosterVersion):
        room_id = str(discussion_id)
        with store.transaction(write=True) as db:
            before = store.snapshot(db, room_id)
            if before is None:
                raise not_found()
            if before["roster_version"] != body.roster_version:
                raise ApiError(409, "ROSTER_VERSION_MISMATCH", "阵容已更新，请刷新后重试。", "roster_version")
            try:
                after, events = request_roster_regeneration(before, body.roster_version)
            except ValueError as error:
                raise ApiError(409, "ROSTER_REGENERATION_NOT_ALLOWED", str(error)) from None
            store.persist(db, before, after, events)
        tasks.notify(room_id)
        tasks.start(room_id)
        return {key: after[key] for key in ("id", "status", "roster_version")}

    @app.post(prefix + "/{discussion_id}/confirm")
    async def confirm(discussion_id: UUID, body: RosterVersion):
        room_id = str(discussion_id)
        with store.transaction(write=True) as db:
            before = store.snapshot(db, room_id)
            if before is None:
                raise not_found()
            if before["started_at"] is None and before["status"] == "awaiting_confirmation":
                if before["roster_version"] != body.roster_version:
                    raise ApiError(409, "ROSTER_VERSION_MISMATCH", "阵容已更新，请刷新后确认。", "roster_version")
            try:
                after, events = confirm_discussion(before, body.roster_version)
            except ValueError as error:
                raise ApiError(409, "ROSTER_NOT_READY", str(error)) from None
            if events:
                store.persist(db, before, after, events)
        if events:
            tasks.notify(room_id)
            tasks.start(room_id)
        return {key: after[key] for key in ("id", "status", "started_at")}

    @app.get(prefix + "/{discussion_id}/events")
    async def subscribe(discussion_id: UUID, request: Request):
        room_id = str(discussion_id)
        raw = request.headers.get("Last-Event-ID") or request.query_params.get("after", "0")
        if not re.fullmatch(r"[0-9]{1,16}", raw) or int(raw) > MAX_CURSOR:
            raise ApiError(422, "VALIDATION_ERROR", "事件游标无效。", "after")
        cursor = int(raw)
        with store.transaction() as db:
            row = db.execute("SELECT event_cursor FROM discussions WHERE id=?", (room_id,)).fetchone()
        if row is None:
            raise not_found()
        if cursor > row["event_cursor"]:
            raise ApiError(409, "CURSOR_OUT_OF_RANGE", "事件游标超出范围，请重新加载讨论。")
        return StreamingResponse(tasks.stream(room_id, cursor), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


app = create_app(turn_pause_seconds=max(0, int(os.getenv("PANEL_TURN_PAUSE_SECONDS", "30"))))
