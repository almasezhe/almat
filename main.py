import asyncio
import logging
import sys
from contextlib import suppress
from os import getenv

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from private.handlers.priv_handler import router as private_router
from group.handlers.pub_handler import router as group_router
from private.handlers.pending_leads import pending_leads_worker

from private.handlers.category_admin import router as category_admin_router
load_dotenv()

TOKEN = getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN not found in .env")


async def main():
    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        )
    )

    dp = Dispatcher()

    dp.include_router(private_router)
    dp.include_router(group_router)
    dp.include_router(category_admin_router)
    await bot.delete_webhook(drop_pending_updates=True)

    worker = asyncio.create_task(pending_leads_worker(bot), name="pending-leads")
    try:
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout
    )

    asyncio.run(main())