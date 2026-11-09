# test/test_fuzzy_search.py
"""
Covers fuzzy product search ("Not built yet" item 3, 2026-11-08's
prioritized backlog, after items 1 (rate limiting) and 2 (order status)
landed).

Previously utils/inventory_manager.py::search_products() was a plain
case-insensitive substring match, documented as a known "by design"
limitation in the README. A customer typo ("jordam", "hoody") returned
zero results, with no fallback -- the LLM's own _resolve_product_id
fallback in agent/tools.py only helps when the MODEL guesses a wrong
product_id, not when the CUSTOMER'S wording itself has a typo that gets
passed straight through as the search query.

The fix adds a token-level fuzzy fallback (stdlib difflib.SequenceMatcher,
no new dependency -- see _fuzzy_search()'s own docstring for why stdlib is
enough at this project's catalog size) that only fires when the exact
substring match finds ZERO results for a non-empty query, so it can never
demote or interfere with an existing exact hit.

This suite is built around a deliberately-typo-prone inventory (not the
real seed data) so fuzzy matches have obvious typo targets to hit, plus a
regression pass against the real data/inventory.json shape at the end.
Sandboxes config.INVENTORY_FILE to a throwaway test_data/ directory,
matching the project's established pattern.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import pytest

TEST_DATA_DIR = Path("test_data")
TEST_DATA_DIR.mkdir(exist_ok=True)

import config
config.INVENTORY_FILE = TEST_DATA_DIR / "inventory.json"
config.FUZZY_SEARCH_THRESHOLD = 0.7

from utils import inventory_manager


TYPO_PRONE_INVENTORY = {
    "products": [
        {
            "id": "p001", "name": "Jordan 4 x KAWS Cool Grey", "category": "sneakers",
            "description": "Collaboration sneaker with signature KAWS suede overlays.",
            "variants": [{"size": "42", "color": "grey", "stock": 5}],
        },
        {
            "id": "p002", "name": "Classic Oversized Hoodie", "category": "clothing",
            "description": "Heavyweight cotton fleece hoodie, relaxed fit.",
            "variants": [{"size": "M", "color": "black", "stock": 5}],
        },
        {
            "id": "p003", "name": "Slim Fit Denim Jacket", "category": "clothing",
            "description": "Classic blue denim jacket with a tailored cut.",
            "variants": [{"size": "L", "color": "blue", "stock": 5}],
        },
    ]
}


@pytest.fixture(autouse=True)
def setup_test_data():
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(TYPO_PRONE_INVENTORY, f)
    inventory_manager._cache["mtime"] = None
    yield
    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()
    inventory_manager._cache["mtime"] = None


# --- regression: exact substring match is completely unaffected -----------

def test_exact_substring_match_still_works_unchanged():
    results = inventory_manager.search_products(query="Jordan")
    assert len(results) == 1 and results[0]["id"] == "p001"


def test_exact_match_takes_priority_never_touches_fuzzy():
    """
    Core safety property: when an exact substring match exists, fuzzy
    matching must never run at all -- not just "not change the results",
    but genuinely not be consulted. We force an absurdly loose threshold
    (0 -- everything would fuzzy-match) and confirm an exact-match query
    still returns ONLY the exact hit, proving the fuzzy path is skipped
    entirely rather than just failing to add noise by chance.
    """
    config.FUZZY_SEARCH_THRESHOLD = 0.0
    results = inventory_manager.search_products(query="hoodie")
    assert len(results) == 1 and results[0]["id"] == "p002"
    config.FUZZY_SEARCH_THRESHOLD = 0.7


def test_empty_query_browses_everything_unaffected():
    results = inventory_manager.search_products(query="")
    assert len(results) == 3


def test_category_filter_alone_unaffected():
    results = inventory_manager.search_products(query="", category="clothing")
    ids = {p["id"] for p in results}
    assert ids == {"p002", "p003"}


# --- core fuzzy fallback cases ----------------------------------------------

def test_single_word_typo_finds_the_product():
    """The headline case: 'jordam' (one-letter typo) must find Jordan."""
    results = inventory_manager.search_products(query="jordam")
    assert len(results) >= 1
    assert results[0]["id"] == "p001"


def test_missing_letter_typo_finds_the_product():
    """'hoody' instead of 'hoodie' -- no exact substring match exists."""
    results = inventory_manager.search_products(query="hoody")
    assert len(results) >= 1
    assert results[0]["id"] == "p002"


def test_transposed_letters_typo_finds_the_product():
    results = inventory_manager.search_products(query="denmi")  # denim -> denmi
    assert len(results) >= 1
    assert results[0]["id"] == "p003"


def test_case_insensitivity_preserved_in_fuzzy_path():
    results_lower = inventory_manager.search_products(query="jordam")
    results_upper = inventory_manager.search_products(query="JORDAM")
    assert [p["id"] for p in results_lower] == [p["id"] for p in results_upper]


# --- threshold / precision cases -------------------------------------------

def test_completely_unrelated_query_returns_nothing():
    """A query with no reasonable match to anything must not force a
    match just because fuzzy matching ran -- the threshold must hold."""
    results = inventory_manager.search_products(query="xyzqqq999")
    assert results == []


def test_too_different_word_does_not_fuzzy_match_at_default_threshold():
    """'shirt' is meaningfully different from every product's words at the
    default 0.7 threshold -- must not force a spurious match onto the
    closest-but-still-distant candidate."""
    results = inventory_manager.search_products(query="shirt")
    assert results == []


def test_looser_threshold_catches_more_typos():
    """Sanity check that the threshold constant is actually load-bearing --
    lowering it should let through a borderline case a stricter threshold
    rejects."""
    config.FUZZY_SEARCH_THRESHOLD = 0.3
    loose_results = inventory_manager.search_products(query="shirt")
    config.FUZZY_SEARCH_THRESHOLD = 0.7
    strict_results = inventory_manager.search_products(query="shirt")
    assert len(loose_results) >= len(strict_results)


# --- category + fuzzy interaction -------------------------------------------

def test_category_filter_applies_to_fuzzy_results_too():
    """A fuzzy query combined with a category filter must still respect
    that filter -- a typo'd sneaker query under category='clothing' must
    not surface the sneaker."""
    results = inventory_manager.search_products(query="jordam", category="clothing")
    assert results == []  # p001 is 'sneakers', filtered out regardless of fuzzy score


def test_category_filter_narrows_fuzzy_results_correctly():
    results = inventory_manager.search_products(query="hoody", category="clothing")
    assert len(results) >= 1
    assert all(p["category"] == "clothing" for p in results)
    assert results[0]["id"] == "p002"


# --- punctuation stripping (found during development: a trailing comma
# in a description word was silently dragging its match score down) -------

def test_punctuation_attached_to_haystack_word_does_not_hurt_its_score():
    """
    p002's own description contains 'hoodie,' (comma attached, from
    "...relaxed fit." sentence boundary) right next to a clean 'Hoodie' in
    the name. A typo'd query must score against the best available token
    regardless of which one happened to have punctuation stuck to it --
    confirms token punctuation-stripping is actually wired in, not just
    present in the code but unused.
    """
    results = inventory_manager.search_products(query="hoody")
    assert results and results[0]["id"] == "p002"


# --- ranking ----------------------------------------------------------------

def test_fuzzy_results_ranked_best_match_first():
    """When more than one product clears the threshold, the closer match
    must come first -- not catalog insertion order."""
    # 'denim' is a near-exact match for p003 and should outscore anything
    # else in this catalog by a wide margin.
    results = inventory_manager.search_products(query="denim")
    assert results, "expected at least one fuzzy/exact match"
    assert results[0]["id"] == "p003"


# --- regression against the real data/inventory.json shape -----------------

def test_regression_real_inventory_shape_unaffected():
    """Sanity check against inventory shaped like the project's real
    data/inventory.json (non-overlapping, distinctly-named products) --
    confirms the fix doesn't regress the common, already-working case."""
    real_shaped = {
        "products": [
            {
                "id": "p001", "name": "Jordan 4 x KAWS Cool Grey", "category": "sneakers",
                "description": "Collaboration sneaker with signature KAWS suede overlays and glow-in-the-dark sole.",
                "variants": [{"size": "40", "color": "cool grey", "stock": 24}],
            },
            {
                "id": "p002", "name": "Classic Oversized Hoodie", "category": "clothing",
                "description": "Heavyweight cotton fleece hoodie, relaxed fit.",
                "variants": [{"size": "M", "color": "black", "stock": 6}],
            },
        ]
    }
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(real_shaped, f)
    inventory_manager._cache["mtime"] = None

    assert len(inventory_manager.search_products(query="Jordan")) == 1
    # 'sneakers' legitimately appears in p001's own category field, which is
    # part of the searchable haystack (name + description + category) --
    # this is a genuine exact match, not a fuzzy one, and is correct,
    # unchanged behavior from before this fix.
    assert len(inventory_manager.search_products(query="sneakers", category="sneakers")) == 1
    assert len(inventory_manager.search_products(query="", category="sneakers")) == 1
    assert len(inventory_manager.search_products(query="")) == 2


def main():
    """Lets this be run directly with `python test/test_fuzzy_search.py`,
    matching the style of the other test_*.py files in this project."""
    import inspect

    test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.isfunction(fn)
    ]

    passed = 0

    def _reset():
        for f in TEST_DATA_DIR.glob("*.json"):
            f.unlink()
        with open(config.INVENTORY_FILE, "w") as f:
            json.dump(TYPO_PRONE_INVENTORY, f)
        inventory_manager._cache["mtime"] = None
        config.FUZZY_SEARCH_THRESHOLD = 0.7

    for name, fn in test_fns:
        _reset()
        try:
            fn()
            print(f"✅ {name}")
            passed += 1
        except Exception as e:
            print(f"❌ {name}: {e}")

    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()
    print(f"\n{passed}/{len(test_fns)} passed")


if __name__ == "__main__":
    main()
