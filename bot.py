import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

import config
from handlers import user_handlers, admin_handlers, channel_images


async def main():
    logging.basicConfig(level=logging.INFO)

    # Optional proxy for networks where api.telegram.org is unreachable
    # directly (e.g. ISP-level filtering). aiohttp does NOT pick up the
    # HTTP(S)_PROXY environment variables by itself, so when a proxy is
    # needed it must be passed explicitly via PROXY_URL in .env.
    # socks5:// URLs additionally require: pip install aiohttp-socks
    # Leave PROXY_URL unset for a direct connection.
    proxy = os.getenv("PROXY_URL") or None
    bot = Bot(
        token=config.BOT_TOKEN,
        session=AiohttpSession(proxy=proxy) if proxy else None,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Admin router first so commands like /pending_orders aren't swallowed
    # by the catch-all text handler in user_handlers. channel_images
    # listens on a different update type entirely (channel_post, not
    # message), so its position relative to the other two doesn't matter
    # functionally -- grouped here with admin_handlers since both are
    # seller-facing, not customer-facing.
    dp.include_router(admin_handlers.router)
    dp.include_router(channel_images.router)
    dp.include_router(user_handlers.router)

    await bot.delete_webhook(drop_pending_updates=True)
    # allowed_updates is left unset: aiogram's Dispatcher auto-resolves
    # which update types to poll for from the handlers actually
    # registered above (dp.resolve_used_update_types()), so adding
    # channel_images' channel_post handler is enough on its own to make
    # Telegram start delivering channel_post updates -- no separate config
    # needed here. Worth a quick live check the first time the
    # product-images channel is used, since this is relying on that
    # auto-resolution behavior rather than an explicit allowed_updates list.
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())