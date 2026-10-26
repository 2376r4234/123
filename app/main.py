"""Точка входа: запуск Telegram-бота и планировщика проверки расписания."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import scheduler as schedule_jobs
from . import storage
from .config import settings
from .handlers import router

logger = logging.getLogger(__name__)


def build_bot() -> Bot:
    if not settings.bot_token:
        raise SystemExit(
            "Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен от @BotFather."
        )
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def start_health_server() -> web.AppRunner | None:
    """Мини HTTP-сервер на порту PORT — нужен хостингам вроде Render.

    Render отдаёт бесплатный Web Service только если процесс слушает порт $PORT.
    Локально переменная PORT не задана, поэтому сервер не поднимается.
    """
    port = os.getenv("PORT")
    if not port:
        return None
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="OK"))
    app.router.add_get("/health", lambda _: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    logger.info("HTTP health-сервер слушает порт %s", port)
    return runner


async def run_bot() -> None:
    storage.init_db()
    bot = build_bot()
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    health_runner = await start_health_server()

    scheduler = AsyncIOScheduler(timezone=datetime.now().astimezone().tzinfo)
    scheduler.add_job(
        schedule_jobs.check_all,
        "interval",
        minutes=settings.check_interval_minutes,
        args=[bot],
        next_run_time=datetime.now(),
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    logger.info(
        "Бот запущен. Школ в конфиге: %d, интервал проверки: %d мин.",
        len(settings.schools),
        settings.check_interval_minutes,
    )
    try:
        await dispatcher.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if health_runner is not None:
            await health_runner.cleanup()
        await bot.session.close()


async def run_check_once() -> None:
    storage.init_db()
    bot = build_bot()
    await schedule_jobs.check_all(bot)
    await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram-бот с расписанием занятий")
    parser.add_argument(
        "--check-once",
        action="store_true",
        help="один раз проверить источники и выйти (без запуска бота)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if args.check_once:
        asyncio.run(run_check_once())
    else:
        asyncio.run(run_bot())


if __name__ == "__main__":
    main()
