# test/test_product_images.py
"""
Covers product images ("Not built yet" item 4, 2026-11-09) including the
same-day extension for per-color photos (Moeid's follow-up once the
single-image version was confirmed working live): a private, bot-admin
Telegram channel used as a seller-facing image-upload gallery. Posting a
photo captioned with a product id (plain or "#"-prefixed) sets that
product's DEFAULT photo; adding a color as a second caption word
("p001 black") sets that color's specific photo instead, validated
against the product's own real variant colors.

Five layers:
  1. handlers/channel_images.py::_parse_caption -- caption parsing (id
     only, id+color, hashtag, missing/blank).
  2. utils/inventory_manager.py::set_product_image /
     get_image_for_color -- the write path (default vs color-specific,
     color validation against real variants, last-write-wins per field)
     and the read/resolution path (color match, fallback-to-default,
     no-image-at-all).
  3. search_products's has_image (true if EITHER image field is set) and
     that neither raw field ever leaks into a search listing.
  4. handlers/channel_images.py::set_product_image_from_channel -- the
     channel listener end-to-end for both caption shapes.
  5. agent/tools.py::_get_product_details (color resolution, AND the
     re-fetch-by-id fix for products resolved via the name/fuzzy search
     fallback path, which strips raw image fields) and
     agent/agent.py's product_image event (now carries color) plus
     handlers/user_handlers.py::_send_product_image's color-aware caption.

Sandboxes config paths to a throwaway test_data/ directory, same pattern
as the other isolated suites.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

TEST_DATA_DIR = Path("test_data")
TEST_DATA_DIR.mkdir(exist_ok=True)
TEST_CONVERSATIONS_DIR = TEST_DATA_DIR / "conversations"
TEST_CONVERSATIONS_DIR.mkdir(exist_ok=True)

import config
config.INVENTORY_FILE = TEST_DATA_DIR / "inventory.json"
config.ORDERS_FILE = TEST_DATA_DIR / "orders.json"
config.FEEDBACK_FILE = TEST_DATA_DIR / "feedback.json"
config.CARTS_FILE = TEST_DATA_DIR / "carts.json"
config.CONVERSATIONS_DIR = TEST_CONVERSATIONS_DIR
config.PRODUCT_IMAGES_CHANNEL_ID = -1005555555

from utils import inventory_manager
from handlers.channel_images import _parse_caption, set_product_image_from_channel
from handlers.user_handlers import _send_product_image
from agent.tools import call_tool
import agent.agent as agent_module
from agent.agent import run_agent


MULTI_COLOR_INVENTORY = {
    "products": [
        {
            "id": "p001", "name": "Test Sneakers", "category": "sneakers",
            "variants": [{"size": "42", "color": "black", "stock": 5}],
        },
        {
            "id": "p002", "name": "Test Hoodie", "category": "clothing",
            "variants": [
                {"size": "M", "color": "black", "stock": 5},
                {"size": "M", "color": "cream", "stock": 5},
            ],
        },
    ]
}


@pytest.fixture(autouse=True)
def setup_test_data():
    with open(config.INVENTORY_FILE, "w") as f:
        json.dump(MULTI_COLOR_INVENTORY, f)
    with open(config.ORDERS_FILE, "w") as f:
        json.dump([], f)
    with open(config.FEEDBACK_FILE, "w") as f:
        json.dump([], f)
    with open(config.CARTS_FILE, "w") as f:
        json.dump({}, f)
    for f in TEST_CONVERSATIONS_DIR.glob("*.json"):
        f.unlink()

    inventory_manager._cache["mtime"] = None

    yield

    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()
    for f in TEST_CONVERSATIONS_DIR.glob("*.json"):
        f.unlink()


# --- _parse_caption: id + optional color ------------------------------

def test_parse_plain_id_no_color():
    assert _parse_caption("p001") == ("p001", None)


def test_parse_hashtag_id_no_color():
    assert _parse_caption("#p001") == ("p001", None)


def test_parse_id_with_color():
    assert _parse_caption("p002 black") == ("p002", "black")


def test_parse_hashtag_id_with_color():
    assert _parse_caption("#p002 black") == ("p002", "black")


def test_parse_ignores_extra_tokens_beyond_color():
    assert _parse_caption("p002 black front view") == ("p002", "black")


def test_parse_none_for_blank_caption():
    assert _parse_caption("") == (None, None)
    assert _parse_caption(None) == (None, None)
    assert _parse_caption("   ") == (None, None)


def test_parse_none_for_bare_hashtag():
    assert _parse_caption("#") == (None, None)


# --- inventory_manager.set_product_image(): default vs color-specific --

@pytest.mark.asyncio
async def test_set_default_image_unchanged_behavior():
    result = await inventory_manager.set_product_image("p001", "AgACdefault")
    assert result["status"] == "ok" and result["color"] is None
    product = inventory_manager.get_product_by_id("p001")
    assert product["image"] == "AgACdefault"


@pytest.mark.asyncio
async def test_set_color_specific_image_success():
    result = await inventory_manager.set_product_image("p002", "AgACblack", color="black")
    assert result["status"] == "ok" and result["color"] == "black"
    product = inventory_manager.get_product_by_id("p002")
    assert product["images_by_color"]["black"] == "AgACblack"
    assert "image" not in product or product.get("image") is None  # default untouched/unset


@pytest.mark.asyncio
async def test_set_color_specific_image_case_insensitive_matching():
    result = await inventory_manager.set_product_image("p002", "AgACblack", color="BLACK")
    assert result["status"] == "ok" and result["color"] == "black"


@pytest.mark.asyncio
async def test_set_color_specific_image_rejects_color_not_on_product():
    result = await inventory_manager.set_product_image("p002", "AgACwrong", color="red")
    assert "error" in result
    assert "red" in result["error"]
    assert "black" in result["error"] and "cream" in result["error"]  # valid colors listed
    product = inventory_manager.get_product_by_id("p002")
    assert "images_by_color" not in product or "red" not in product.get("images_by_color", {})


@pytest.mark.asyncio
async def test_set_image_unknown_product():
    result = await inventory_manager.set_product_image("p999", "x")
    assert "error" in result


@pytest.mark.asyncio
async def test_default_and_color_specific_coexist_independently():
    await inventory_manager.set_product_image("p002", "AgACdefault")
    await inventory_manager.set_product_image("p002", "AgACblack", color="black")
    product = inventory_manager.get_product_by_id("p002")
    assert product["image"] == "AgACdefault"
    assert product["images_by_color"]["black"] == "AgACblack"
    assert "cream" not in product["images_by_color"]


@pytest.mark.asyncio
async def test_last_write_wins_per_field_independently():
    await inventory_manager.set_product_image("p002", "black_v1", color="black")
    await inventory_manager.set_product_image("p002", "black_v2", color="black")
    await inventory_manager.set_product_image("p002", "cream_v1", color="cream")
    product = inventory_manager.get_product_by_id("p002")
    assert product["images_by_color"]["black"] == "black_v2"
    assert product["images_by_color"]["cream"] == "cream_v1"


# --- inventory_manager.get_image_for_color(): resolution logic ---------

@pytest.mark.asyncio
async def test_get_image_for_color_returns_exact_match():
    await inventory_manager.set_product_image("p002", "AgACdefault")
    await inventory_manager.set_product_image("p002", "AgACblack", color="black")
    product = inventory_manager.get_product_by_id("p002")

    image, resolved = inventory_manager.get_image_for_color(product, "black")
    assert image == "AgACblack" and resolved == "black"


@pytest.mark.asyncio
async def test_get_image_for_color_falls_back_to_default_when_no_color_match():
    await inventory_manager.set_product_image("p002", "AgACdefault")
    product = inventory_manager.get_product_by_id("p002")

    image, resolved = inventory_manager.get_image_for_color(product, "cream")
    assert image == "AgACdefault" and resolved is None  # fallback, not a real color match


def test_get_image_for_color_no_color_requested_returns_default():
    product = {"id": "p002", "image": "AgACdefault"}
    image, resolved = inventory_manager.get_image_for_color(product, None)
    assert image == "AgACdefault" and resolved is None


def test_get_image_for_color_nothing_at_all():
    product = {"id": "p999"}
    image, resolved = inventory_manager.get_image_for_color(product, "black")
    assert image is None and resolved is None


# --- search_products: has_image + no raw leak of EITHER field ----------

@pytest.mark.asyncio
async def test_has_image_true_from_default_only():
    await inventory_manager.set_product_image("p001", "AgACdefault")
    results = inventory_manager.search_products(query="Sneakers")
    assert results[0]["has_image"] is True
    assert "image" not in results[0] and "images_by_color" not in results[0]


@pytest.mark.asyncio
async def test_has_image_true_from_color_specific_only():
    await inventory_manager.set_product_image("p002", "AgACblack", color="black")
    results = inventory_manager.search_products(query="Hoodie")
    assert results[0]["has_image"] is True
    assert "image" not in results[0] and "images_by_color" not in results[0]


def test_has_image_false_when_neither_field_set():
    results = inventory_manager.search_products(query="Hoodie")
    assert results[0]["has_image"] is False


# --- handlers/channel_images.py: end-to-end for both caption shapes -----

def _channel_message(chat_id, caption, file_id="file_abc", media_group_id=None):
    photo = [SimpleNamespace(file_id=file_id)]
    replies = []

    async def answer(text):
        replies.append(text)

    msg = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id), caption=caption, photo=photo,
        media_group_id=media_group_id, answer=answer,
    )
    msg._replies = replies
    return msg


@pytest.mark.asyncio
async def test_channel_handler_sets_default_image():
    msg = _channel_message(config.PRODUCT_IMAGES_CHANNEL_ID, "p001", file_id="AgACreal")
    await set_product_image_from_channel(msg)

    assert any("✅" in r and "Default" in r for r in msg._replies)
    assert inventory_manager.get_product_by_id("p001")["image"] == "AgACreal"


@pytest.mark.asyncio
async def test_channel_handler_sets_color_specific_image():
    msg = _channel_message(config.PRODUCT_IMAGES_CHANNEL_ID, "p002 black", file_id="AgACblk")
    await set_product_image_from_channel(msg)

    assert any("✅" in r and "black" in r for r in msg._replies)
    assert inventory_manager.get_product_by_id("p002")["images_by_color"]["black"] == "AgACblk"


@pytest.mark.asyncio
async def test_channel_handler_rejects_invalid_color_with_clear_message():
    msg = _channel_message(config.PRODUCT_IMAGES_CHANNEL_ID, "p002 purple", file_id="AgACbad")
    await set_product_image_from_channel(msg)

    assert any("⚠️" in r and "purple" in r for r in msg._replies)
    product = inventory_manager.get_product_by_id("p002")
    assert "purple" not in product.get("images_by_color", {})


@pytest.mark.asyncio
async def test_channel_handler_ignores_wrong_channel():
    msg = _channel_message(-999999999, "p001")
    await set_product_image_from_channel(msg)
    assert msg._replies == []
    assert inventory_manager.get_product_by_id("p001").get("image") is None


@pytest.mark.asyncio
async def test_channel_handler_ignores_media_group_album():
    msg = _channel_message(config.PRODUCT_IMAGES_CHANNEL_ID, "p001", media_group_id="mg_123")
    await set_product_image_from_channel(msg)
    assert msg._replies == []


# --- agent/tools.py::_get_product_details: color resolution + the ------
# --- re-fetch-by-id fix for the search-fallback path --------------------

@pytest.mark.asyncio
async def test_get_product_details_direct_id_no_color_returns_default():
    await inventory_manager.set_product_image("p002", "AgACdefault")
    result = await call_tool("get_product_details", {"product_id": "p002"}, 1, "user")
    assert result["image"] == "AgACdefault"
    assert "image_color" not in result


@pytest.mark.asyncio
async def test_get_product_details_direct_id_with_color_match():
    await inventory_manager.set_product_image("p002", "AgACdefault")
    await inventory_manager.set_product_image("p002", "AgACblack", color="black")
    result = await call_tool("get_product_details", {"product_id": "p002", "color": "black"}, 1, "user")
    assert result["image"] == "AgACblack"
    assert result["image_color"] == "black"


@pytest.mark.asyncio
async def test_get_product_details_color_with_no_specific_photo_falls_back():
    await inventory_manager.set_product_image("p002", "AgACdefault")
    result = await call_tool("get_product_details", {"product_id": "p002", "color": "cream"}, 1, "user")
    assert result["image"] == "AgACdefault"
    assert "image_color" not in result  # fallback, not a genuine match


@pytest.mark.asyncio
async def test_get_product_details_never_exposes_raw_images_by_color_map():
    await inventory_manager.set_product_image("p002", "AgACblack", color="black")
    await inventory_manager.set_product_image("p002", "AgACcream", color="cream")
    result = await call_tool("get_product_details", {"product_id": "p002", "color": "black"}, 1, "user")
    assert "images_by_color" not in result


@pytest.mark.asyncio
async def test_get_product_details_via_name_fallback_still_resolves_real_image():
    """
    THE core regression this fix exists for: when _resolve_product_id
    takes the search_products fallback path (customer/model passed a name
    instead of the exact id), the returned product is a search-result
    shallow copy with image fields already stripped. Without the
    re-fetch-by-id in _get_product_details, this would make a product
    LOOK imageless even though it genuinely has a photo on file -- a real
    bug caught by reasoning through the code, not by a test failure, so
    this test exists specifically to make sure it stays fixed.
    """
    await inventory_manager.set_product_image("p002", "AgACdefault")
    # "Hoodie" is not p002's exact id -- this forces _resolve_product_id's
    # search_products() fallback path (single match by name).
    result = await call_tool("get_product_details", {"product_id": "Hoodie"}, 1, "user")
    assert result["image"] == "AgACdefault"


# --- handlers/user_handlers.py::_send_product_image: color-aware caption

@pytest.mark.asyncio
async def test_send_product_image_caption_includes_color_when_present():
    bot = SimpleNamespace(send_photo=AsyncMock())
    event = {"type": "product_image", "data": {"image": "AgACblack", "name": "Test Hoodie", "color": "black"}}
    await _send_product_image(bot, 123, event)
    bot.send_photo.assert_awaited_once_with(123, "AgACblack", caption="Test Hoodie (black)")


@pytest.mark.asyncio
async def test_send_product_image_caption_is_just_name_when_no_color():
    bot = SimpleNamespace(send_photo=AsyncMock())
    event = {"type": "product_image", "data": {"image": "AgACdefault", "name": "Test Sneakers", "color": None}}
    await _send_product_image(bot, 123, event)
    bot.send_photo.assert_awaited_once_with(123, "AgACdefault", caption="Test Sneakers")


# --- agent/agent.py: product_image event carries color end-to-end ------

def _message(content, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


class _FakeMessageDump(SimpleNamespace):
    def model_dump(self, exclude_unset=False):
        tool_calls = None
        if self.tool_calls:
            tool_calls = [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in self.tool_calls
            ]
        return {"role": "assistant", "content": self.content, "tool_calls": tool_calls}


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


@pytest.mark.asyncio
async def test_product_image_event_carries_resolved_color(monkeypatch):
    await inventory_manager.set_product_image("p002", "AgACdefault")
    await inventory_manager.set_product_image("p002", "AgACblack", color="black")

    tool_calls = [_tool_call("call_1", "get_product_details", {"product_id": "p002", "color": "black"})]
    round1 = SimpleNamespace(choices=[SimpleNamespace(message=_FakeMessageDump(content=None, tool_calls=tool_calls))])
    round2 = SimpleNamespace(choices=[SimpleNamespace(message=_message(content="Here's the black one!", tool_calls=None))])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(side_effect=[round1, round2])
    )))
    monkeypatch.setattr(agent_module, "get_client", lambda: (fake_client, "fake-model"))

    reply, events = await run_agent(user_id=1, username="alice", user_message="show me the black hoodie")

    product_image_events = [e for e in events if e["type"] == "product_image"]
    assert len(product_image_events) == 1
    assert product_image_events[0]["data"]["image"] == "AgACblack"
    assert product_image_events[0]["data"]["color"] == "black"


@pytest.mark.asyncio
async def test_product_image_event_color_is_none_on_fallback(monkeypatch):
    await inventory_manager.set_product_image("p002", "AgACdefault")

    tool_calls = [_tool_call("call_1", "get_product_details", {"product_id": "p002", "color": "cream"})]
    round1 = SimpleNamespace(choices=[SimpleNamespace(message=_FakeMessageDump(content=None, tool_calls=tool_calls))])
    round2 = SimpleNamespace(choices=[SimpleNamespace(message=_message(content="Here's a general photo.", tool_calls=None))])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(side_effect=[round1, round2])
    )))
    monkeypatch.setattr(agent_module, "get_client", lambda: (fake_client, "fake-model"))

    reply, events = await run_agent(user_id=2, username="bob", user_message="show me the cream hoodie")

    product_image_events = [e for e in events if e["type"] == "product_image"]
    assert len(product_image_events) == 1
    assert product_image_events[0]["data"]["image"] == "AgACdefault"
    assert product_image_events[0]["data"]["color"] is None


def main():
    """Lets this be run directly with `python test/test_product_images.py`."""
    import asyncio
    import inspect

    sync_test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.isfunction(fn) and not inspect.iscoroutinefunction(fn)
    ]
    async_test_fns = [
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and inspect.iscoroutinefunction(fn)
    ]

    class _Monkeypatch:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._undo):
                setattr(obj, name, value)

    passed = 0
    total = len(sync_test_fns) + len(async_test_fns)

    def _reset():
        for f in TEST_DATA_DIR.glob("*.json"):
            f.unlink()
        with open(config.INVENTORY_FILE, "w") as f:
            json.dump(MULTI_COLOR_INVENTORY, f)
        with open(config.ORDERS_FILE, "w") as f:
            json.dump([], f)
        with open(config.FEEDBACK_FILE, "w") as f:
            json.dump([], f)
        with open(config.CARTS_FILE, "w") as f:
            json.dump({}, f)
        for f in TEST_CONVERSATIONS_DIR.glob("*.json"):
            f.unlink()
        inventory_manager._cache["mtime"] = None

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

    for f in TEST_DATA_DIR.glob("*.json"):
        f.unlink()
    for f in TEST_CONVERSATIONS_DIR.glob("*.json"):
        f.unlink()
    print(f"\n{passed}/{total} passed")


if __name__ == "__main__":
    main()