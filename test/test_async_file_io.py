# test_async_file_io.py
"""
Covers Issue #16 (bug-audit 3.4): "Synchronous file I/O blocks the event
loop."

Before this fix, every manager (cart_manager, order_manager,
conversation_manager, inventory_manager) called plain blocking
open()/json.load/json.dump directly inside async functions. Since
asyncio's event loop is single-threaded, a slow disk write stalled every
other coroutine on the loop — including aiogram's polling of every other
user's Telegram updates — for the full duration of that write.

The fix introduces utils/file_io.py: read_json/write_json_atomic offload
the actual blocking work to a worker thread via asyncio.to_thread(), and
writes are atomic (temp file + os.replace) so a concurrent reader can
never observe a half-written file — a hazard that specifically only
exists *because* writes now happen off the event-loop thread (previously
a read could never physically overlap a write, since both ran on the same
single thread).

This suite is layered:
  1. test_event_loop_stays_responsive_during_slow_write — the actual bug
     this issue describes: proves a slow write no longer blocks other
     coroutines from running concurrently.
  2. test_concurrent_readers_and_writers_never_see_a_torn_file — the
     specific new hazard this fix has to guard against, now that writes
     aren't confined to the loop thread: many concurrent readers/writers
     hammering the same file must never hit a JSONDecodeError.
  3. test_write_json_atomic_sync_* — unit tests of the atomic-write helper
     itself: no leftover temp files, retries past a transient
     PermissionError (the Windows os.replace-contention case named in the
     issue/backlog doc), and still raises if every retry is exhausted.
  4. test_read_json_sync_missing_file_returns_default — regression check
     for the FileNotFoundError-swallowing behavior every manager's old
     `_load` already had.

Sandboxes to a throwaway test_data/ directory, same pattern as the other
isolated suites in this project (test_stock_reversal.py,
test/test_backend.py) — this file writes its own throwaway JSON file
directly via utils.file_io, not through any of the four managers, so it
needs no config.*_FILE redirection; it never touches real data/.
"""
import sys
import os
import json
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import pytest

from utils import file_io

TEST_DATA_DIR = Path("test_data")
TEST_DATA_DIR.mkdir(exist_ok=True)


@pytest.fixture(autouse=True)
def setup_test_data():
    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()
    yield
    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()
    # Clean up any leftover atomic-write temp files from a failed-retry test.
    for f in TEST_DATA_DIR.glob(".tmp-*"):
        f.unlink()


# --- 1. the actual bug: a slow write must not block the event loop --------

@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_slow_write(monkeypatch):
    """
    Core Issue #16 case. Patches write_json_atomic_sync to sleep (simulating
    slow disk I/O) and runs it concurrently with a coroutine that increments
    a counter on every loop tick. If the write still ran on the event-loop
    thread (the pre-fix behavior), the counter would be frozen for the
    entire sleep duration — the loop can't do anything else while a
    blocking call is in progress. Since write_json_atomic offloads to a
    worker thread via asyncio.to_thread(), the loop keeps ticking the
    counter the whole time instead.
    """
    path = TEST_DATA_DIR / "slow_write.json"
    sleep_seconds = 0.3

    real_write = file_io.write_json_atomic_sync

    def slow_write(p, data):
        time.sleep(sleep_seconds)
        real_write(p, data)

    monkeypatch.setattr(file_io, "write_json_atomic_sync", slow_write)

    ticks = {"count": 0}
    stop = asyncio.Event()

    async def ticker():
        while not stop.is_set():
            ticks["count"] += 1
            await asyncio.sleep(0.01)

    ticker_task = asyncio.create_task(ticker())
    await file_io.write_json_atomic(path, {"hello": "world"})
    stop.set()
    await ticker_task

    # ~0.3s of sleeping at a ~0.01s tick interval should yield roughly 25-30
    # ticks if the loop was free to run the ticker concurrently. A generous
    # floor (5) avoids flakiness on a slow CI box while still being utterly
    # impossible if the write had actually blocked the loop (which would
    # yield 0 or 1 ticks, since the ticker couldn't run at all until the
    # blocking call returned).
    assert ticks["count"] >= 5, (
        f"event loop only ticked {ticks['count']} times during a "
        f"{sleep_seconds}s write — the write appears to be blocking the loop"
    )

    # The write itself must still have actually completed correctly.
    with open(path) as f:
        assert json.load(f) == {"hello": "world"}


# --- 2. the new hazard this fix must close: torn reads ---------------------

@pytest.mark.asyncio
async def test_concurrent_readers_and_writers_never_see_a_torn_file():
    """
    Once writes run on a worker thread instead of the event-loop thread, a
    read can genuinely overlap a write in time for the first time — unlike
    before this fix, where both always ran on the same single thread and a
    read could never physically interleave with a write mid-flight. This
    is exactly the hazard atomic writes (temp file + os.replace) exist to
    close: a reader must always see either the fully-old or fully-new file,
    never a partially-written one. Hammers one shared file with many
    concurrent readers and writers and asserts not a single read ever
    raises JSONDecodeError or sees a malformed/partial structure.
    """
    path = TEST_DATA_DIR / "contended.json"
    await file_io.write_json_atomic(path, {"n": 0, "payload": "x" * 2000})

    errors: list[Exception] = []

    async def writer(n: int):
        try:
            await file_io.write_json_atomic(path, {"n": n, "payload": "y" * 2000})
        except Exception as e:  # pragma: no cover - failure path surfaced via errors list
            errors.append(e)

    async def reader():
        try:
            data = await file_io.read_json(path, None)
            # A torn/partial write would produce either a JSONDecodeError
            # (caught structurally here since read_json would propagate it)
            # or, worse, a parseable-but-incomplete dict missing "n" — guard
            # against both.
            assert data is not None
            assert "n" in data
            assert "payload" in data
        except Exception as e:
            errors.append(e)

    tasks = [writer(i) for i in range(40)] + [reader() for _ in range(40)]
    await asyncio.gather(*tasks)

    assert errors == [], f"concurrent readers/writers hit {len(errors)} error(s): {errors[:3]}"

    # File must still be valid, complete JSON after the dust settles.
    final = await file_io.read_json(path, None)
    assert final is not None and "n" in final and "payload" in final


# --- 3. atomic-write helper unit tests -------------------------------------

def test_write_json_atomic_sync_leaves_no_temp_files():
    path = TEST_DATA_DIR / "clean_write.json"
    file_io.write_json_atomic_sync(path, {"a": 1})

    assert path.exists()
    with open(path) as f:
        assert json.load(f) == {"a": 1}

    leftover_temp_files = list(TEST_DATA_DIR.glob(".tmp-*"))
    assert leftover_temp_files == [], f"atomic write left temp file(s) behind: {leftover_temp_files}"


def test_write_json_atomic_sync_overwrites_existing_file_completely():
    """A later write must fully replace earlier content, not merge with it
    — regression check that os.replace() behaves as a real swap, not an
    append."""
    path = TEST_DATA_DIR / "overwrite.json"
    file_io.write_json_atomic_sync(path, {"version": 1, "big": "x" * 5000})
    file_io.write_json_atomic_sync(path, {"version": 2})

    with open(path) as f:
        data = json.load(f)
    assert data == {"version": 2}


def test_write_json_atomic_sync_retries_past_transient_permission_error():
    """
    Mirrors the Windows-specific case named in the issue/backlog doc:
    os.replace() can raise PermissionError if another handle has the
    destination file open at that exact instant. The helper should retry a
    few times rather than failing on the first transient error.
    """
    path = TEST_DATA_DIR / "retry_write.json"
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("simulated: another handle has the file open")
        return real_replace(src, dst)

    with patch("utils.file_io.os.replace", side_effect=flaky_replace):
        with patch("utils.file_io.time.sleep", return_value=None):  # don't actually wait in tests
            file_io.write_json_atomic_sync(path, {"ok": True})

    assert calls["n"] == 3, "expected exactly 2 failed attempts before the 3rd succeeded"
    with open(path) as f:
        assert json.load(f) == {"ok": True}
    assert list(TEST_DATA_DIR.glob(".tmp-*")) == []


def test_write_json_atomic_sync_raises_and_cleans_up_after_exhausting_retries():
    """If os.replace() never succeeds, the helper must eventually raise
    (not silently drop the write) and must not leave the temp file
    behind."""
    path = TEST_DATA_DIR / "always_fails.json"

    def always_fails(src, dst):
        raise PermissionError("simulated: permanently locked")

    with patch("utils.file_io.os.replace", side_effect=always_fails):
        with patch("utils.file_io.time.sleep", return_value=None):
            with pytest.raises(PermissionError):
                file_io.write_json_atomic_sync(path, {"never": "written"})

    assert not path.exists()
    assert list(TEST_DATA_DIR.glob(".tmp-*")) == [], "failed write must not leave a temp file behind"


# --- 4. regression: missing-file default behavior unchanged ---------------

def test_read_json_sync_missing_file_returns_default():
    path = TEST_DATA_DIR / "does_not_exist.json"
    assert file_io.read_json_sync(path, []) == []
    assert file_io.read_json_sync(path, {}) == {}
    assert file_io.read_json_sync(path, "sentinel") == "sentinel"


@pytest.mark.asyncio
async def test_read_json_missing_file_returns_default_async():
    path = TEST_DATA_DIR / "also_does_not_exist.json"
    assert await file_io.read_json(path, []) == []


def main():
    """Lets this be run directly with `python test/test_async_file_io.py`,
    matching the style of the other test_*.py files in this project."""
    import inspect

    async_test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.iscoroutinefunction(fn)
    ]
    sync_test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.isfunction(fn) and not inspect.iscoroutinefunction(fn)
    ]

    passed = 0
    total = len(async_test_fns) + len(sync_test_fns)

    def _reset():
        for f in TEST_DATA_DIR.glob("*.json"):
            f.unlink()
        for f in TEST_DATA_DIR.glob(".tmp-*"):
            f.unlink()

    class _Monkeypatch:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._undo):
                setattr(obj, name, value)

    for name, fn in sync_test_fns:
        _reset()
        try:
            fn()
            print(f"✅ {name}")
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")

    for name, fn in async_test_fns:
        _reset()
        needs_monkeypatch = "monkeypatch" in inspect.signature(fn).parameters
        mp = _Monkeypatch() if needs_monkeypatch else None
        try:
            if needs_monkeypatch:
                asyncio.run(fn(mp))
            else:
                asyncio.run(fn())
            print(f"✅ {name}")
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")
        finally:
            if mp:
                mp.undo()

    _reset()
    print(f"\n{passed}/{total} passed")


if __name__ == "__main__":
    main()
