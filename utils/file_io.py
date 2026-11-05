"""
Shared helpers for async, atomic JSON file I/O.

Issue #16 ("Synchronous file I/O blocks the event loop" — bug-audit 3.4):
every manager (cart_manager, order_manager, conversation_manager,
inventory_manager) previously called plain open()/json.load/json.dump
directly inside async functions. Python's asyncio event loop is
single-threaded, so any blocking disk call stalls the whole loop —
including aiogram's polling of every other user's updates — for the
duration of that call.

This module provides the two building blocks each manager now uses:

  - read_json_sync / write_json_atomic_sync: the actual blocking file
    operations, meant to be run inside asyncio.to_thread(), never called
    directly from a coroutine.
  - read_json / write_json_atomic: async wrappers that offload to a worker
    thread via asyncio.to_thread().

Atomic writes matter here specifically *because* of the move to threads.
Before this fix, every read and every write ran on the single event-loop
thread, so a read could never observe a write that was only half-finished.
Once writes run in a background thread (while the loop thread keeps
servicing other coroutines, including other reads), that guarantee is
gone — a naive `open(path, "w")` + `json.dump()` truncates the file before
writing the new content, so a concurrent read landing in that window would
hit a JSONDecodeError on a torn/partial file. This is the same failure
class already documented for conversation_manager pre-Issue-3.3, just
reachable a new way.

write_json_atomic_sync avoids this by writing to a temporary file in the
same directory, then atomically replacing the target with os.replace().
Any reader either sees the old complete file or the new complete file,
never a partial one.

**Read-side retry — found via real concurrent testing on Windows, not
theoretical.** Even with the write side atomic, a concurrent
`open(path, "r")` can transiently raise PermissionError
(OSError errno 13, "the process cannot access the file because it is
being used by another process") while another thread's os.replace() is
in flight — observed directly under this project's own concurrent
read/write stress test (test/test_async_file_io.py) on a Windows venv: 3
failures out of 80 concurrent operations hammering one file, all
PermissionError from the read path, which previously had no retry logic
at all (only the write side retried around os.replace). This is a
Windows-specific sharing-violation window, not data corruption — the file
itself is never torn, since the write really is atomic — so a short,
bounded retry on the read side resolves it the same way it already did on
the write side. POSIX doesn't have this failure mode (rename() there
doesn't block concurrent opens), but the retry is harmless and nearly
instant when it isn't needed.

Locking is UNCHANGED by this module. Each manager still owns its own
asyncio.Lock (_carts_lock, _orders_lock, _history_lock, _inventory_lock)
and is responsible for acquiring it before calling these helpers — this
module has no locking of its own. For cart_manager, order_manager, and
conversation_manager, every read AND write to a given file already goes
through that same lock, so in practice there's only ever one in-flight
file operation per file at a time for those three — the retry logic here
exists mainly for inventory_manager, whose read helpers
(get_product_by_id/search_products/check_variant, built on
_load_if_changed) are deliberately NOT locked (see inventory_manager.py's
module docstring) and so can genuinely race decrement_stock_for_cart's/
restore_stock_for_order's locked, now-threaded writes.
"""
import json
import os
import time
import asyncio
import tempfile

# A reader or writer hitting a mid-replace file on Windows is expected to
# be rare and short-lived — os.replace() is effectively instantaneous once
# both files exist — but under heavy concurrent contention (many readers
# and writers hitting the same path at once) it can take a handful of
# retries to clear. Exponential backoff (capped) keeps the common,
# uncontended case essentially free while still giving heavy contention
# enough total wall-clock time to resolve.
_REPLACE_MAX_RETRIES = 10
_REPLACE_RETRY_BASE_DELAY_SECONDS = 0.005
_REPLACE_RETRY_MAX_DELAY_SECONDS = 0.05

_READ_MAX_RETRIES = 10
_READ_RETRY_BASE_DELAY_SECONDS = 0.005
_READ_RETRY_MAX_DELAY_SECONDS = 0.05


def _backoff_delay(attempt: int, base: float, cap: float) -> float:
    return min(base * (2 ** attempt), cap)


def read_json_sync(path, default):
    """
    Blocking JSON read. Returns `default` if the file doesn't exist yet
    (matches the FileNotFoundError-swallowing behavior every manager
    already had). Must only be called via asyncio.to_thread() from a
    coroutine — never awaited/called directly, since it blocks.

    Retries a bounded number of times on PermissionError — see the "Read-
    side retry" note in the module docstring for why this is real and
    Windows-specific, not theoretical. FileNotFoundError is NOT retried;
    it's treated as "no file yet" and returns `default` immediately, since
    a genuinely missing file isn't a transient condition that resolves on
    its own.
    """
    last_error = None
    for attempt in range(_READ_MAX_RETRIES):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except PermissionError as e:
            last_error = e
            time.sleep(_backoff_delay(attempt, _READ_RETRY_BASE_DELAY_SECONDS, _READ_RETRY_MAX_DELAY_SECONDS))
    raise last_error


def write_json_atomic_sync(path, data):
    """
    Blocking atomic JSON write: serialize to a temp file in the same
    directory as `path`, then os.replace() it into place. A reader can
    never observe a partially-written file, which matters once writes run
    on a worker thread (see module docstring). Must only be called via
    asyncio.to_thread() — never directly from a coroutine.
    """
    directory = os.path.dirname(str(path)) or "."
    # NamedTemporaryFile with delete=False: we need the file to survive
    # being closed so os.replace() can pick it up, and we clean it up
    # ourselves on both the success and failure paths.
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        last_error = None
        for attempt in range(_REPLACE_MAX_RETRIES):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError as e:
                # Windows-only in practice: another handle has `path` open.
                # Short retry, then give up and surface the real error.
                last_error = e
                time.sleep(_backoff_delay(attempt, _REPLACE_RETRY_BASE_DELAY_SECONDS, _REPLACE_RETRY_MAX_DELAY_SECONDS))
        raise last_error
    except Exception:
        # Only reached if dump/fsync failed, or every replace retry failed —
        # in both cases the temp file is still sitting on disk and should
        # not be left behind.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def read_json(path, default):
    """Async wrapper: offloads read_json_sync to a worker thread so the
    event loop keeps servicing other coroutines while disk I/O happens.
    Caller is responsible for holding whatever lock guards `path`."""
    return await asyncio.to_thread(read_json_sync, path, default)


async def write_json_atomic(path, data):
    """Async wrapper: offloads write_json_atomic_sync to a worker thread.
    Caller is responsible for holding whatever lock guards `path`."""
    await asyncio.to_thread(write_json_atomic_sync, path, data)