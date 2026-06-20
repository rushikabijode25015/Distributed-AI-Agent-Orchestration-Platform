import sys
import os
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor

# ==============================================================================
# IIT-GRADE PYTHON MONKEYPATCHING LAYER
# Runs the full platform on host Windows: SQLite + In-Memory PubSub + ThreadPool
# No Docker, Redis, or PostgreSQL required.
# ==============================================================================

# 1. Mock pgvector so SQLAlchemy accepts Vector columns using SQLite TEXT storage
import types
import sqlalchemy

class MockVector(sqlalchemy.types.TypeDecorator):
    impl = sqlalchemy.Text
    cache_ok = True
    def process_bind_param(self, value, dialect):
        return json.dumps(value) if value is not None else None
    def process_result_value(self, value, dialect):
        return json.loads(value) if value is not None else None

pgvector_mock = types.ModuleType("pgvector")
pgvector_sqla_mock = types.ModuleType("pgvector.sqlalchemy")
pgvector_sqla_mock.Vector = lambda dim: MockVector
sys.modules["pgvector"] = pgvector_mock
sys.modules["pgvector.sqlalchemy"] = pgvector_sqla_mock

# 2. Point all services to local SQLite file
os.environ["DATABASE_URL"] = "sqlite:///./local_dev.db"
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"

# 3. Setup shared output directory for plots & reports (Windows-safe)
SHARED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shared")
os.makedirs(SHARED_DIR, exist_ok=True)

# 4. Build a thread-safe in-memory Pub/Sub hub to replace Redis Pub/Sub
# The event loop reference is stored globally and set before server starts
_event_loop = None

class InMemoryPubSub:
    def __init__(self):
        self.subscribers: dict[str, list] = {}

    def subscribe(self, channel_name: str):
        queue = asyncio.Queue()
        self.subscribers.setdefault(channel_name, []).append(queue)
        return queue

    def unsubscribe(self, channel_name: str, queue):
        if channel_name in self.subscribers:
            try:
                self.subscribers[channel_name].remove(queue)
            except ValueError:
                pass

    def publish(self, channel_name: str, data: str):
        """Called from worker threads — schedules delivery onto the async event loop."""
        if _event_loop is None:
            return
        for queue in list(self.subscribers.get(channel_name, [])):
            asyncio.run_coroutine_threadsafe(queue.put(data), _event_loop)

pubsub_hub = InMemoryPubSub()

# 5. Mock synchronous Redis (used by app/cache.py and app/worker.py)
class MockRedisSync:
    def __init__(self, *args, **kwargs):
        self._store: dict = {}

    def ping(self):
        return True

    def keys(self, pattern: str):
        prefix = pattern.replace("*", "")
        return [k for k in self._store if k.startswith(prefix)]

    def get(self, key: str):
        return self._store.get(key)

    def setex(self, key: str, expiry: int, value: str):
        self._store[key] = value

    def publish(self, channel: str, message):
        if isinstance(message, bytes):
            message = message.decode()
        pubsub_hub.publish(channel, message)

# 6. Mock async Redis + PubSub (used by app/main.py WebSocket gateway)
class AsyncMockPubSubHandle:
    def __init__(self):
        self._channel = None
        self._queue = None

    async def subscribe(self, channel: str):
        self._channel = channel
        self._queue = pubsub_hub.subscribe(channel)

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        try:
            data = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            return {"data": data}
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError()

    async def unsubscribe(self, channel: str):
        if self._queue:
            pubsub_hub.unsubscribe(channel, self._queue)

    async def close(self):
        pass

class AsyncMockRedis:
    def __init__(self, *args, **kwargs):
        self._pubsub = None

    def pubsub(self):
        return self

    async def subscribe(self, channel: str):
        self._pubsub = AsyncMockPubSubHandle()
        await self._pubsub.subscribe(channel)

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        return await self._pubsub.get_message(ignore_subscribe_messages, timeout)

    async def unsubscribe(self, channel: str):
        if self._pubsub:
            await self._pubsub.unsubscribe(channel)

    async def close(self):
        if self._pubsub:
            await self._pubsub.close()

# Patch both sync and async Redis
import redis as _redis_sync
import redis.asyncio as _redis_async
_redis_sync.Redis = MockRedisSync
_redis_async.Redis = AsyncMockRedis

# 7. Patch database.init_db to skip CREATE EXTENSION (PostgreSQL-only syntax)
from app import database as _db_module
from app.config import settings as _settings

def _local_init_db():
    print("[Local DB] Building SQLite schema (skipping pgvector extension)...")
    _db_module.Base.metadata.create_all(bind=_db_module.engine)
    print("[Local DB] Schema ready.")

_db_module.init_db = _local_init_db

# 8. Patch the sandbox output path to use local shared dir (not /app/shared)
import app.agents.sandbox as _sandbox_module
_orig_run_sandbox = _sandbox_module.run_sandbox_agent

def _patched_run_sandbox(db, task_id):
    # Temporarily override shared dir detection
    _sandbox_module.shared_dir_override = SHARED_DIR
    _orig_run_sandbox(db, task_id)

# Patch shared_dir inside sandbox at runtime by setting env var
os.environ["LOCAL_SHARED_DIR"] = SHARED_DIR

# 9. Patch sandbox.py to use LOCAL_SHARED_DIR env var at runtime
import app.agents.sandbox as sandbox_mod

_orig_run_sandbox_agent = sandbox_mod.run_sandbox_agent

def _patched_sandbox_agent(db, task_id):
    """Wrapper that injects local shared dir before running sandbox agent."""
    import app.agents.sandbox as _s
    _original_exists = os.path.exists

    # temporarily redirect /app/shared checks to our local shared folder
    def _mock_exists(path):
        if path == "/app/shared":
            return False  # Force fallback to local path
        return _original_exists(path)

    os.path.exists = _mock_exists
    try:
        _orig_run_sandbox_agent(db, task_id)
    finally:
        os.path.exists = _original_exists

sandbox_mod.run_sandbox_agent = _patched_sandbox_agent

# Patch writer to also use local shared dir
import app.agents.writer as writer_mod
_orig_run_writer_agent = writer_mod.run_writer_agent

def _patched_writer_agent(db, task_id):
    import os as _os
    _original_exists = _os.path.exists

    def _mock_exists(path):
        if path == "/app/shared":
            return False
        return _original_exists(path)

    _os.path.exists = _mock_exists
    try:
        _orig_run_writer_agent(db, task_id)
    finally:
        _os.path.exists = _original_exists

writer_mod.run_writer_agent = _patched_writer_agent

# 10. Setup thread pool for executing Celery tasks locally
executor = ThreadPoolExecutor(max_workers=4)

from app.worker import run_agent_task
import app.worker as worker_mod
import app.orchestrator as orch_mod

def _mock_delay(task_id: str):
    print(f"[LocalScheduler] Queuing task {task_id} -> ThreadPoolExecutor")
    executor.submit(run_agent_task, task_id)

run_agent_task.delay = _mock_delay
worker_mod.run_agent_task.delay = _mock_delay

# 11. Serve local static files from ./shared
from app.main import app as fastapi_app
from fastapi.staticfiles import StaticFiles

# Re-mount /shared to our local windows directory
# (remove any existing mount first)
for route in list(fastapi_app.routes):
    if hasattr(route, "name") and route.name == "shared":
        fastapi_app.routes.remove(route)
        break

fastapi_app.mount("/shared", StaticFiles(directory=SHARED_DIR), name="shared")

# 12. Launch
import uvicorn

if __name__ == "__main__":
    print("=" * 54)
    print("  Distributed AI Agent Orchestration Platform")
    print("  Mode  : Local Simulator (SQLite + ThreadPool)")
    print(f"  Output: {SHARED_DIR}")
    print("  URL   : http://localhost:8000/frontend/index.html")
    print("=" * 54)

    _event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_event_loop)

    _local_init_db()

    config = uvicorn.Config(
        app=fastapi_app,
        host="127.0.0.1",
        port=8000,
        loop="none",       # We supply our own loop
        log_level="info"
    )
    server = uvicorn.Server(config)
    _event_loop.run_until_complete(server.serve())
