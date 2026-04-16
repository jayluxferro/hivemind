"""Tests for the checkpoint store."""

import pytest

from hivemind.execution.checkpoint import Checkpoint, CheckpointStore


@pytest.fixture
def store(tmp_path):
    return CheckpointStore(checkpoint_dir=str(tmp_path / "checkpoints"))


@pytest.mark.asyncio
async def test_save_and_load(store):
    cp = Checkpoint(
        agent_id="agent-1",
        task_id="task-1",
        state={"step": 3, "context": "some context"},
        stdout_so_far="partial output",
        tokens_used=5000,
    )

    path = await store.save(cp)
    assert path.endswith(".json")

    loaded = await store.load("agent-1")
    assert loaded is not None
    assert loaded.agent_id == "agent-1"
    assert loaded.task_id == "task-1"
    assert loaded.state["step"] == 3
    assert loaded.tokens_used == 5000


@pytest.mark.asyncio
async def test_load_by_path(store):
    cp = Checkpoint(
        agent_id="agent-2",
        task_id="task-2",
        state={"progress": 50},
    )
    path = await store.save(cp)

    loaded = await store.load_by_path(path)
    assert loaded is not None
    assert loaded.agent_id == "agent-2"
    assert loaded.state["progress"] == 50


@pytest.mark.asyncio
async def test_load_nonexistent(store):
    loaded = await store.load("nonexistent")
    assert loaded is None


@pytest.mark.asyncio
async def test_list_checkpoints(store):
    for i in range(3):
        await store.save(Checkpoint(
            agent_id=f"agent-{i}",
            task_id="task-1",
            state={"i": i},
        ))

    all_cps = await store.list_checkpoints()
    assert len(all_cps) == 3

    filtered = await store.list_checkpoints(task_id="task-1")
    assert len(filtered) == 3

    filtered = await store.list_checkpoints(task_id="nonexistent")
    assert len(filtered) == 0


@pytest.mark.asyncio
async def test_delete(store):
    await store.save(Checkpoint(
        agent_id="del-agent",
        task_id="task-1",
        state={},
    ))

    assert await store.load("del-agent") is not None
    deleted = await store.delete("del-agent")
    assert deleted is True
    assert await store.load("del-agent") is None


@pytest.mark.asyncio
async def test_cleanup(store):
    import time

    cp = Checkpoint(
        agent_id="old-agent",
        task_id="task-1",
        state={},
        created_at=time.time() - 200000,  # Very old
    )
    await store.save(cp)

    removed = await store.cleanup(max_age=100000)
    assert removed == 1
