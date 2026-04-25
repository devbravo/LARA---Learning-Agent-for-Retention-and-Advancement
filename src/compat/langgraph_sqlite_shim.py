from typing import Dict, Iterator, Sequence

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    CheckpointTuple,
    CheckpointMetadata,
)

try:
    from langgraph.pregel._checkpoint import empty_checkpoint
except Exception:
    empty_checkpoint = None


class SqliteSaver(BaseCheckpointSaver):
    """A lightweight in-memory SqliteSaver-compatible shim.

    This class exists to provide compatibility with code/tests that import
    ``langgraph.checkpoint.sqlite.SqliteSaver``. The real implementation
    may persist checkpoints to SQLite; this shim stores checkpoints in a
    simple in-memory mapping keyed by thread id. The constructor accepts a
    ``sqlite3.Connection`` for API compatibility but does not use it.
    """

    def __init__(self, conn=None, *, serde=None):
        super().__init__(serde=serde)
        self.conn = conn
        self._store: Dict[str, CheckpointTuple] = {}

    def get_tuple(self, config) -> CheckpointTuple | None:
        thread_id = (config or {}).get("configurable", {}).get("thread_id")
        if thread_id is None:
            return None
        return self._store.get(thread_id)

    def put(
        self,
        config,
        checkpoint,
        metadata: CheckpointMetadata,
        new_versions,
    ):
        thread_id = (config or {}).get("configurable", {}).get("thread_id")
        if thread_id is None:
            return config
        tuple_value = CheckpointTuple(config, checkpoint, metadata)
        self._store[thread_id] = tuple_value
        return config

    def list(self, config=None, *, filter=None, before=None, limit=None) -> Iterator[CheckpointTuple]:
        for v in self._store.values():
            yield v

    def delete_thread(self, thread_id: str) -> None:
        self._store.pop(thread_id, None)

    def put_writes(self, config, writes: Sequence[tuple[str, object]], task_id: str, task_path: str = "") -> None:
        # No-op for in-memory shim
        return None

    # Provide synchronous convenience methods expected by some callers
    def save(self, thread_id: str, checkpoint) -> None:
        # Store without explicit metadata for the convenience helper.
        self._store[thread_id] = CheckpointTuple({"configurable": {"thread_id": thread_id}}, checkpoint, None)

    def load(self, thread_id: str):
        val = self._store.get(thread_id)
        return val.checkpoint if val is not None else None




