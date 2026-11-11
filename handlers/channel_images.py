"""
Product-images ingestion channel ("Not built yet" item 4, 2026-11-09,
extended the same day to support per-color photos): a dedicated private
Telegram channel the bot is an ADMIN of, used purely as a seller-facing
photo gallery. The seller posts a photo captioned with the product id --
optionally followed by a color, to set that color's specific photo rather
than the product's general/default one.

Caption formats accepted (a leading '#' on the id is always optional):
  "p001"           -> sets p001's DEFAULT photo
  "#p001"          -> same
  "p002 black"     -> sets p002's photo specifically for "black"
  "#p002 black"    -> same

The color token (if present) is validated against that product's own
real variant colors by inventory_manager.set_product_image() -- a typo
("blak") is rejected with the valid colors listed in the channel reply,
rather than silently creating a dead entry no lookup could ever reach.

Why a channel rather than an admin command: Telegram channels already
give a seller a natural batch-upload, scroll-through gallery UI for
free -- a one-at-a-time command can't match that for someone
photographing a whole catalog, color variants included. The channel is
purely a better INGESTION experience; once a file_id is captured, it's
stored in the exact same fields, and sent via the exact same
bot.send_photo() call, that a hand-edited image URL would use (see
inventory_manager.py's module docstring).

Trust model: Telegram only ever delivers channel_post updates for
channels the bot is an ADMIN of, and only channel admins/owners can post
in a channel to begin with -- the real access control is already
enforced by Telegram itself. The config.PRODUCT_IMAGES_CHANNEL_ID check
below exists only so a post from some OTHER channel can never be
mistaken for a trusted image upload -- a belt-and-suspenders identity
check, not the actual security boundary.

Scope, deliberately narrow (unchanged from the first version):
  - ONE photo per post -- media-group (album) posts are ignored with a
    warning, since Telegram doesn't reliably attach a caption to every
    photo in a group.
  - Last write always wins for whichever field (default or a specific
    color) a post targets. No history/versioning.
  - This channel is the only ingestion path -- no separate admin command.
    A seller who'd rather hand-edit inventory.json directly can still set
    `image` or `images_by_color` fields themselves; both converge on the
    same storage.
"""
import logging
from aiogram import Router, F
from aiogram.types import Message

import config
from utils import inventory_manager

logger = logging.getLogger("channel_images")

router = Router()


def _parse_caption(caption: str | None) -> tuple[str | None, str | None]:
    """
    Parses a channel post's caption into (product_id, color). Accepts a
    plain or hashtag-style product id as the first token (leading '#'
    stripped either way), with an optional color as the second token --
    anything beyond that (a seller's own notes) is ignored.

    Returns (None, None) if the caption is missing/blank or the first
    token reduces to nothing usable (e.g. a caption that's just "#").
    """
    if not caption or not caption.strip():
        return None, None
    tokens = caption.strip().split()
    product_id = tokens[0].lstrip("#").strip() or None
    if not product_id:
        return None, None
    color = tokens[1].strip() if len(tokens) > 1 else None
    return product_id, (color or None)


@router.channel_post(F.photo)
async def set_product_image_from_channel(message: Message):
    """
    Fires on every photo posted in the configured product-images channel.
    See module docstring for the full design.
    """
    if config.PRODUCT_IMAGES_CHANNEL_ID and message.chat.id != config.PRODUCT_IMAGES_CHANNEL_ID:
        return  # not the configured image channel -- ignore silently

    if message.media_group_id:
        logger.warning(
            "Ignoring photo in a media group (album) in the product-images channel -- "
            "only single-photo posts are supported. chat_id=%s", message.chat.id,
        )
        return

    product_id, color = _parse_caption(message.caption)
    if not product_id:
        logger.warning(
            "Product-images channel post with no usable caption/product id. chat_id=%s",
            message.chat.id,
        )
        await message.answer(
            "⚠️ Couldn't read a product id from that caption -- post the photo again "
            "with the product's id as the caption (e.g. 'p001'), optionally followed "
            "by a color for a color-specific photo (e.g. 'p001 black')."
        )
        return

    file_id = message.photo[-1].file_id  # largest size, same convention as handle_payment_screenshot
    result = await inventory_manager.set_product_image(product_id, file_id, color=color)

    if result.get("error"):
        logger.warning("Failed to set image for '%s' (color=%r): %s", product_id, color, result["error"])
        await message.answer(f"⚠️ Couldn't set image: {result['error']}")
        return

    if result.get("color"):
        await message.answer(
            f"✅ Image set for {product_id} ({result['product_name']}) -- color: {result['color']}."
        )
    else:
        await message.answer(f"✅ Default image set for {product_id} ({result['product_name']}).")