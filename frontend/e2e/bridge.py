"""Run the real FastAPI app and forward finite SSE windows over stdio for Chromium."""

import asyncio
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

from backend.main import create_app
from backend.tests.integration_model import OfflineModel


class ControlledModel(OfflineModel):
    def __init__(self):
        super().__init__()
        self.gates = {}
        self.lock = Lock()
        self.stopping = False

    def gate(self, topic, turn):
        with self.lock:
            gate = self.gates.setdefault((topic, turn), Event())
            if self.stopping:
                gate.set()
            return gate

    def release_all(self):
        with self.lock:
            self.stopping = True
            for gate in self.gates.values():
                gate.set()

    def __call__(self, operation, **context):
        topic = context["topic"]
        transcript = context.get("transcript", [])
        experts = sum(item["kind"] == "expert" for item in transcript)
        if operation == "intentions" and experts in (0, 2):
            if not self.gate(topic, experts).wait(30):
                raise RuntimeError("Test did not release the model checkpoint")
        result = super().__call__(operation, **context)
        if operation == "utterance":
            result["text"] = f"{len(transcript) + 1}．{result['text']}"
        return result


async def request(app, item):
    messages = []
    started = False
    disconnect = asyncio.Event()
    streaming = item["path"].endswith("/events")

    async def receive():
        nonlocal started
        if not started:
            started = True
            return {"type": "http.request", "body": (item.get("body") or "").encode()}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "scheme": "http", "method": item["method"],
        "path": item["path"], "root_path": "", "query_string": item["query"].encode(),
        "headers": [(name.lower().encode(), value.encode()) for name, value in item["headers"].items()],
        "server": ("localhost", 5173), "client": ("127.0.0.1", 12345),
    }
    # Playwright route.fulfill requires a finite response. Forward every real frame
    # collected in this window unchanged; the browser then reconnects normally.
    timer = asyncio.get_running_loop().call_later(0.20, disconnect.set) if streaming else None
    try:
        await app(scope, receive, send)
    finally:
        if timer:
            timer.cancel()
    head, *chunks = messages
    return {
        "status": head["status"],
        "headers": {
            key.decode(): value.decode() for key, value in head["headers"] if key != b"content-length"
        },
        "body": b"".join(message.get("body", b"") for message in chunks).decode(),
    }


async def serve():
    model = ControlledModel()
    with tempfile.TemporaryDirectory(prefix="panel-e2e-") as directory:
        app = create_app(Path(directory) / "test.sqlite3", model=model, turn_pause_seconds=0)

        async def reply(command):
            try:
                if command["op"] == "release":
                    model.gate(command["topic"], command["turn"]).set()
                    response = {}
                else:
                    response = await request(app, command)
                print(json.dumps({"id": command["id"], **response}), flush=True)
            except Exception as error:
                print(json.dumps({"id": command["id"], "error": repr(error)}), flush=True)

        async with app.router.lifespan_context(app):
            outstanding = set()
            while line := await asyncio.to_thread(sys.stdin.readline):
                command = json.loads(line)
                if command["op"] == "stop":
                    break
                task = asyncio.create_task(reply(command))
                outstanding.add(task)
                task.add_done_callback(outstanding.discard)
            model.release_all()
            await asyncio.gather(*outstanding)


if __name__ == "__main__":
    if os.getenv("PANEL_E2E_NO_LOOPBACK") == "1" and sys.platform == "win32":
        # Restricted Windows runtimes may also block asyncio's internal socket pair.
        class PipeLoop(asyncio.ProactorEventLoop):
            def _make_self_pipe(self):
                self.call_later(0.005, self._tick)

            def _tick(self):
                self.call_later(0.005, self._tick)

            def _close_self_pipe(self):
                pass

            def _write_to_self(self):
                pass

            def _loop_self_reading(self, future=None):
                pass

        with ThreadPoolExecutor(max_workers=1) as pool:
            if sys.version_info >= (3, 12):
                pool.submit(lambda: asyncio.run(serve(), loop_factory=PipeLoop)).result()
            else:
                policy = asyncio.WindowsProactorEventLoopPolicy()
                policy._loop_factory = PipeLoop
                asyncio.set_event_loop_policy(policy)
                pool.submit(lambda: asyncio.run(serve())).result()
    else:
        asyncio.run(serve())
