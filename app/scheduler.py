"""Периодическая проверка источников и рассылка расписания подписчикам."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from html import escape
from io import BytesIO
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from PIL import Image

from . import schedule_source, schedule_split, storage
from .config import Channel, settings

logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
MAX_IMAGE_SIDE = 5000
MAX_IMAGE_SUM = 9500

WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def format_date_title(file_name: str) -> str:
    published = schedule_source.parse_date(file_name)
    if not published:
        return file_name
    return f"{published.strftime('%d.%m.%Y')} ({WEEKDAYS[published.weekday()]})"


def _read_text(path) -> str:
    return Path(path).read_text(encoding="utf-8-sig", errors="replace")


def _school_groups(channel: Channel, path) -> list[str]:
    """Список групп: для PDF — из таблиц документа, для таблицы — из CSV.

    Для таблицы берём все группы из всех секций, чтобы в списке были доступны и те,
    у кого в текущей секции (например, во временных изменениях) занятий нет.
    """
    if channel.source == "google_sheet":
        return list(schedule_source.parse_sheet(_read_text(path))[0])
    return schedule_split.extract_groups(path)


async def check_all(bot: Bot) -> None:
    for channel in settings.channels():
        try:
            await check_channel(bot, channel)
        except Exception:
            logger.exception("Не удалось проверить школу «%s»", channel.name)


async def check_channel(bot: Bot, channel: Channel) -> None:
    files = await asyncio.to_thread(schedule_source.list_files, channel)
    known = await asyncio.to_thread(storage.known_file_ids, channel.id)

    if not known:
        await asyncio.to_thread(storage.mark_files_seen, channel.id, files)
        await refresh_cache(channel, files)
        logger.info(
            "Школа «%s»: первый запуск, запомнено файлов — %d", channel.name, len(files)
        )
        return

    new_files = [item for item in files if item.id not in known]
    today = date.today()
    for source_file in new_files:
        if source_file.published and source_file.published < today:
            continue
        await notify_file(bot, channel, source_file)

    if new_files:
        await asyncio.to_thread(storage.mark_files_seen, channel.id, new_files)
    await refresh_cache(channel, files)


async def notify_file(bot: Bot, channel: Channel, source_file: schedule_source.SourceFile) -> None:
    subscribers = await asyncio.to_thread(storage.get_subscribers, channel.id)
    if not subscribers:
        logger.info(
            "Новый файл «%s» у школы «%s», но подписчиков нет",
            source_file.name,
            channel.name,
        )
        return

    path = await asyncio.to_thread(
        schedule_source.download, source_file, settings.cache_dir(channel.id)
    )
    groups = set(await asyncio.to_thread(_school_groups, channel, path))
    if not groups:
        logger.warning("В файле «%s» не найдено ни одной группы", source_file.name)
        return

    logger.info("Рассылаю «%s» (%d подписчиков)", source_file.name, len(subscribers))
    for subscriber in subscribers:
        group = subscriber["group_name"]
        if group not in groups:
            continue
        try:
            await send_group_photo(
                bot,
                subscriber["user_id"],
                channel,
                path,
                group,
                source_file.name,
                header="Новое расписание",
            )
        except Exception:
            logger.exception(
                "Не удалось отправить расписание пользователю %s", subscriber["user_id"]
            )
        await asyncio.sleep(0.05)


async def get_school_schedule(channel: Channel) -> dict | None:
    cache = await asyncio.to_thread(storage.get_cache, channel.id)
    if (
        cache
        and cache["groups"]
        and cache["file_path"]
        and Path(cache["file_path"]).exists()
    ):
        return cache

    try:
        files = await asyncio.to_thread(schedule_source.list_files, channel)
    except Exception:
        logger.exception("Не удалось получить список файлов школы «%s»", channel.name)
        return cache

    return await refresh_cache(channel, files)


async def refresh_cache(channel: Channel, files: list[schedule_source.SourceFile]) -> dict | None:
    if not files:
        return None

    relevant = schedule_source.pick_relevant(files)
    cache = await asyncio.to_thread(storage.get_cache, channel.id)
    if (
        cache
        and cache["file_id"] == relevant.id
        and cache["file_path"]
        and Path(cache["file_path"]).exists()
    ):
        return cache

    path = await asyncio.to_thread(
        schedule_source.download, relevant, settings.cache_dir(channel.id)
    )
    groups = await asyncio.to_thread(_school_groups, channel, path)
    await asyncio.to_thread(
        storage.save_cache, channel.id, relevant.id, relevant.name, path, groups
    )
    logger.info(
        "Кэш школы «%s» обновлён: %s (%d групп)", channel.name, relevant.name, len(groups)
    )
    return {
        "file_id": relevant.id,
        "file_name": relevant.name,
        "file_path": path,
        "groups": groups,
    }


def _render_sheet_group(
    path, group: str, title: str, section: str | None, bells: dict | None, sat_bells: dict | None
) -> Image.Image | None:
    _, data = schedule_source.parse_sheet(_read_text(path), section)
    blocks = data.get(group)
    if not blocks:
        return None
    return schedule_split.render_sheet_image(blocks, title, bells, sat_bells)


async def send_group_photo(
    bot: Bot,
    chat_id: int,
    channel: Channel,
    file_path,
    group: str,
    file_name: str,
    header: str,
) -> bool:
    label = format_date_title(file_name)
    suffix = f"\n{label}" if label != channel.name else ""
    title = f"{channel.name} · группа {group}{suffix}"
    if channel.source == "google_sheet":
        image = await asyncio.to_thread(
            _render_sheet_group,
            file_path,
            group,
            title,
            channel.section,
            channel.bells,
            channel.saturday_bells,
        )
    else:
        image = await asyncio.to_thread(
            schedule_split.render_group_image, file_path, group, title
        )

    if image is None:
        await bot.send_message(
            chat_id, escape(f"В расписании нет занятий для группы {group}.")
        )
        return False

    caption_tail = f" · {label}" if label != channel.name else ""
    caption = escape(
        f"{header}\n{channel.name} · группа {group}{caption_tail}"
    )
    photo = BufferedInputFile(
        _to_png_bytes(image), filename=f"{channel.id.replace(':', '_')}_{group}.png"
    )

    previous_message_id = await asyncio.to_thread(
        storage.get_last_message, chat_id, channel.id
    )
    try:
        message = await bot.send_photo(chat_id, photo, caption=caption[:CAPTION_LIMIT])
    except TelegramAPIError:
        logger.warning("send_photo не сработал, отправляю файлом", exc_info=True)
        message = await bot.send_document(chat_id, photo, caption=caption[:CAPTION_LIMIT])

    await asyncio.to_thread(
        storage.save_last_message, chat_id, channel.id, message.message_id
    )
    await _delete_previous(bot, chat_id, previous_message_id)
    return True


async def _delete_previous(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        logger.info("Прошлое расписание (message_id=%s) удалить не удалось", message_id)


def _to_png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    _limit_size(image).save(buffer, format="PNG", compress_level=6)
    return buffer.getvalue()


def _limit_size(image: Image.Image) -> Image.Image:
    scale = min(
        MAX_IMAGE_SUM / (image.width + image.height), MAX_IMAGE_SIDE / max(image.size), 1.0
    )
    if scale >= 1.0:
        return image
    return image.resize(
        (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
        Image.LANCZOS,
    )
