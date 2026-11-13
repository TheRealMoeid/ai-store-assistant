# test_inventory_read_retry.py
"""
Covers a gap found during Issue #16 / bug-audit 3.4 follow-up review (not
caught by the original `python -m pytest . -v` live run, and not covered
by test/test_async_file_io.py, which tests utils/file_io.py in isolation
rather than any of the four managers that actually call it).

The bug: utils/inventory_manager.py::_load_if_changed() is the exact
reader utils/file_io.py's own module docstring names as the motivating
case for adding a bounded retry around transient Windows PermissionError
(it's unlocked, called constantly from agent/tools.py, and can genuinely
land mid-os.replace() while decrement_stock_for_cart()/
restore_stock_for_order() are writing on a worker thread) — but
_load_if_changed() had its own bare open()/json.load() call that never
actually routed through file_io.read_json_sync(), so it never got that
retry protection in the first place. An unhandled PermissionError there
propagates up through get_product_by_id/search_products/check_variant
into agent/tools.py, where call_tool()'s broad `except Exception` catches
it and returns a spurious "tool failed: [Errno 13] ..." to the customer
for what should have been an ordinary read — not a crash, but exactly the
kind of flakiness under concurrent load Issue #16 was meant to close, not
reintroduce.

The fix: _load_if_changed()'s inner open()/json.load() now calls
file_io.read_json_sync() instead, so it gets the same bounded,
exponential-backoff PermissionError retry as every other reader. The outer
mtime check (config.INVENTORY_FILE.stat().st_mtime) is unchanged.

This suite verifies the fix directly by simulating the real Windows race:
patching builtins.open to raise PermissionError a bounded number of times
before succeeding, scoped only to the inventory file path so unrelated
opens (fixture setup/teardown, other test infrastructure) are unaffected.

Sandboxes config.INVENTORY_FILE to a throwaway test_data/ directory,
matching the project's established pattern, so real data/inventory.json
is never touched.
"""
import sys
import os
import builtins
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import pytest

TEST_DATA_DIR = Path("test_data")
TEST_DATA_DIR.mkdir(exist_ok=True)

import config
config.INVENTORY_FILE = TEST_DATA_DIR / "inventory.json"

from utils import inventory_manager, file_io

ONE_PRODUCT_INVENTORY = {
    "products": [
        {
            "id": "p001", "name": "Test Sneakers", "category": "sneakers",
            "variants": [{"size": "42", "color": "black", "stock": 5}],
        }
    ]
}


@pytest.fixture(autouse=True)
def setup_test_data():
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(ONE_PRODUCT_INVENTORY, f)
    inventory_manager._cache["mtime"] = None
    yield
    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()
    inventory_manager._cache["mtime"] = None


def _patch_flaky_open(monkeypatch, path, fail_times: int):
    """
    Makes builtins.open raise PermissionError the first `fail_times` times
    it's called for exactly `path` (as a real Windows os.replace() would
    transiently do to a concurrent reader), succeeding normally after
    that and for every other path. Also patches file_io.time.sleep to a
    no-op so the test doesn't actually wait out the real backoff delays.
    """
    real_open = builtins.open
    calls = {"n": 0}

    def flaky_open(p, *a, **k):
        if str(p) == str(path) and calls["n"] < fail_times:
            calls["n"] += 1
            raise PermissionError(13, "simulated: file in use by another process")
        return real_open(p, *a, **k)

    monkeypatch.setattr(builtins, "open", flaky_open)
    monkeypatch.setattr(file_io.time, "sleep", lambda s: None)
    return calls


# --- core fix: _load_if_changed() survives a transient read-side race ----

def test_load_if_changed_retries_past_transient_permission_error(monkeypatch):
    """
    Core regression case: simulates the real Windows race this fix closes
    — open() transiently denied twice (as if another thread's os.replace()
    were in flight), succeeding on the third attempt. Before the fix (bare
    open()/json.load() with no retry), this would raise PermissionError
    straight out of _load_if_changed() on the very first call. After the
    fix, it must transparently retry and return the correct data.
    """
    calls = _patch_flaky_open(monkeypatch, config.INVENTORY_FILE, fail_times=2)

    products = inventory_manager._load_if_changed()

    assert products == ONE_PRODUCT_INVENTORY["products"]
    assert calls["n"] == 2, "expected exactly 2 failed attempts before the 3rd succeeded"


def test_get_product_by_id_survives_the_same_race(monkeypatch):
    """
    Same race, but exercised through the public call sites agent/tools.py
    actually uses, since those are what a customer-facing failure would
    look like in practice (a spurious "tool failed: ..." error reaching
    the customer) — not just the internal helper in isolation.
    """
    calls = _patch_flaky_open(monkeypatch, config.INVENTORY_FILE, fail_times=2)

    product = inventory_manager.get_product_by_id("p001")

    assert product is not None and product["id"] == "p001"
    assert calls["n"] == 2


def test_search_products_survives_the_same_race(monkeypatch):
    calls = _patch_flaky_open(monkeypatch, config.INVENTORY_FILE, fail_times=2)

    results = inventory_manager.search_products(query="Sneakers")

    assert len(results) == 1 and results[0]["id"] == "p001"
    assert calls["n"] == 2


def test_check_variant_survives_the_same_race(monkeypatch):
    calls = _patch_flaky_open(monkeypatch, config.INVENTORY_FILE, fail_times=2)

    variants = inventory_manager.check_variant("p001", size="42", color="black")

    assert len(variants) == 1 and variants[0]["stock"] == 5
    assert calls["n"] == 2


# --- regression: a real, non-transient error must still surface ----------

def test_load_if_changed_still_raises_if_retries_are_exhausted(monkeypatch):
    """
    This fix must not turn a genuinely broken/permanently-locked file into
    a silent infinite retry or a swallowed error — if the PermissionError
    never clears, it must still propagate once file_io's bounded retry
    budget is exhausted, exactly like every other reader in this project.
    """
    calls = _patch_flaky_open(monkeypatch, config.INVENTORY_FILE, fail_times=10_000)

    with pytest.raises(PermissionError):
        inventory_manager._load_if_changed()


# --- regression: normal, uncontended reads are completely unaffected -----

def test_load_if_changed_unaffected_when_nothing_is_contending():
    """Plain, uncontended read must behave exactly as before this fix —
    no retry overhead, no behavior change, same return shape."""
    products = inventory_manager._load_if_changed()
    assert products == ONE_PRODUCT_INVENTORY["products"]

    # Cache must actually be populated/used — a second call with an
    # unchanged mtime should return the same (cached) object, not re-read.
    products_again = inventory_manager._load_if_changed()
    assert products_again == products


def test_load_if_changed_still_reloads_on_real_inventory_edit():
    """
    Regression check for the mtime-based cache invalidation itself (not
    touched by this fix): editing the file for real (as the seller would,
    by hand) must still be picked up on the next call.

    The rewrite is followed by an explicit os.utime() so the mtime is
    guaranteed to differ from the cached one. Without this, two writes
    within the same filesystem timestamp tick (common on CI runners) leave
    the mtime unchanged and the cache correctly-but-unhelpfully stays put.
    """
    inventory_manager._load_if_changed()  # warm the cache
    original_mtime = config.INVENTORY_FILE.stat().st_mtime

    updated = {
        "products": [
            {
                "id": "p001", "name": "Test Sneakers", "category": "sneakers",
                "variants": [{"size": "42", "color": "black", "stock": 999}],
            }
        ]
    }
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(updated, f)

    later = original_mtime + 2  # well past any filesystem's timestamp granularity
    os.utime(config.INVENTORY_FILE, (later, later))

    products = inventory_manager._load_if_changed()
    assert products[0]["variants"][0]["stock"] == 999


def main():
    """Lets this be run directly with `python test/test_inventory_read_retry.py`,
    matching the style of the other test_*.py files in this project."""
    import inspect

    class _Monkeypatch:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._undo):
                setattr(obj, name, value)

    test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.isfunction(fn)
    ]

    passed = 0
    for name, fn in test_fns:
        for f in TEST_DATA_DIR.glob("*.json"):
            f.unlink()
        with open(config.INVENTORY_FILE, "w") as f:
            json.dump(ONE_PRODUCT_INVENTORY, f)
        inventory_manager._cache["mtime"] = None

        needs_monkeypatch = "monkeypatch" in inspect.signature(fn).parameters
        mp = _Monkeypatch() if needs_monkeypatch else None
        try:
            if needs_monkeypatch:
                fn(mp)
            else:
                fn()
            print(f"✅ {name}")
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")
        finally:
            if mp:
                mp.undo()

    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()
    inventory_manager._cache["mtime"] = None
    print(f"\n{passed}/{len(test_fns)} passed")


if __name__ == "__main__":
    main()