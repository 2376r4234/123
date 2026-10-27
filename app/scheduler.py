"""Периодическая проверка источников и рассылка расписания подписчикам."""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from datetime import date
from html import escape
from io import BytesIO
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
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


def _sheet_groups(path) -> list[str]:
    """Все группы Google Таблицы из всех секций.

    Берём группы из всех секций, чтобы в списке были доступны и те, у кого
    в текущей секции (например, во временных изменениях) занятий нет.
    """
    return list(schedule_source.parse_sheet(_read_text(path))[0])


def group_images_dir(channel: Channel, file_id: str) -> Path:
    """Папка с готовыми картинками групп для конкретного файла расписания."""
    return settings.cache_dir(channel.id) / f"{file_id[:16]}_groups"


def group_title(channel: Channel, group: str, file_name: str) -> str:
    """Подпись к картинке группы: школа, группа и дата расписания."""
    label = format_date_title(file_name)
    suffix = f"\n{label}" if label != channel.name else ""
    return f"{channel.name} · группа {group}{suffix}"


_locks_guard = threading.Lock()
_dir_locks: dict[str, threading.Lock] = {}


def _dir_lock(path: Path) -> threading.Lock:
    """Замок на папку с картинками: две одновременные нарезки одного файла не нужны."""
    key = str(path)
    with _locks_guard:
        lock = _dir_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _dir_locks[key] = lock
        return lock


def ensure_group_images(
    channel: Channel, file_id: str, pdf_path: Path, file_name: str
) -> dict[str, Path]:
    """Готовит картинки всех групп файла один раз и кладёт их в кэш.

    Нарезка PDF — самая тяжёлая операция, поэтому она выполняется однократно при
    появлении нового файла, а не заново при каждом открытии группы. Замок нужен,
    чтобы запрос из бота и планер не запустили нарезку одновременно: на слабом
    хостинге две параллельные нарезки подряд надолго занимают процессор.
    """
    out_dir = group_images_dir(channel, file_id)
    with _dir_lock(out_dir):
        ready = {path.stem: path for path in sorted(out_dir.glob("*.png"))}
        if ready:
            return ready
        for stale in settings.cache_dir(channel.id).glob("*_groups"):
            if stale != out_dir:
                shutil.rmtree(stale, ignore_errors=True)
        return schedule_split.split_pdf_images(
            pdf_path, out_dir, title_for=lambda group: group_title(channel, group, file_name)
        )


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
        # После перезапуска хостинга база на диске теряется, поэтому файл, который
        # уже лежит в источнике, попадает в «первый запуск» и раньше молча
        # запоминался. Если это расписание на сегодня или на будущее — рассылаем
        # его, иначе подписчики не узнают об обновлении.
        relevant = schedule_source.pick_relevant(files)
        if (
            relevant is not None
            and relevant.published is not None
            and relevant.published >= date.today()
        ):
            await notify_file(bot, channel, relevant)
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
    if channel.source == "google_sheet":
        groups = set(await asyncio.to_thread(_sheet_groups, path))
        images_dir = None
    else:
        images = await asyncio.to_thread(
            ensure_group_images, channel, source_file.id, path, source_file.name
        )
        groups = set(images)
        images_dir = group_images_dir(channel, source_file.id)
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
                images_dir=images_dir,
            )
        except Exception:
            logger.exception(
                "Не удалось отправить расписание пользователю %s", subscriber["user_id"]
            )
        await asyncio.sleep(0.05)


def _cache_is_usable(cache: dict | None) -> bool:
    """Кэш пригоден, если файл на месте и это не расписание прошедшего дня.

    Расписание с датой в прошлом считается устаревшим: пока планер не успел
    обновиться (он проверяет источник раз в полчаса), пользователь иначе получил бы
    вчерашний день. Такие случаи заставляют бота лишний раз сходить в источник.
    """
    if not (cache and cache["groups"] and cache["file_path"] and Path(cache["file_path"]).exists()):
        return False
    published = schedule_source.parse_date(cache["file_name"] or "")
    return published is None or published >= date.today()


async def get_school_schedule(channel: Channel, target_date: date | None = None) -> dict | None:
    cache = await asyncio.to_thread(storage.get_cache, channel.id)
    # Если запрашивается конкретная дата, кэш можно использовать только если он совпадает по дате
    if not target_date and _cache_is_usable(cache):
        if channel.source != "google_sheet" and cache["file_id"]:
            try:
                await asyncio.to_thread(
                    ensure_group_images,
                    channel,
                    cache["file_id"],
                    Path(cache["file_path"]),
                    cache["file_name"],
                )
            except Exception:
                logger.exception("Не удалось подготовить картинки школы «%s»", channel.name)
        return cache

    try:
        files = await asyncio.to_thread(schedule_source.list_files, channel)
    except Exception:
        logger.exception("Не удалось получить список файлов школы «%s»", channel.name)
        return cache

    return await refresh_cache(channel, files, target_date)


async def refresh_cache(
    channel: Channel, files: list[schedule_source.SourceFile], target_date: date | None = None
) -> dict | None:
    if not files:
        return None

    if target_date and channel.source != "google_sheet":
        relevant = schedule_source.find_for_date(files, target_date)
        if not relevant:
            return None
    else:
        relevant = schedule_source.pick_relevant(files)

    # Если мы обновляем основной кэш (нет target_date), проверяем, не тот же ли это файл
    if not target_date:
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
    if channel.source == "google_sheet":
        groups = await asyncio.to_thread(_sheet_groups, path)
    else:
        images = await asyncio.to_thread(
            ensure_group_images, channel, relevant.id, path, relevant.name
        )
        groups = list(images)

    # Сохраняем в БД только «основное» (наиболее актуальное) расписание
    if not target_date:
        await asyncio.to_thread(
            storage.save_cache, channel.id, relevant.id, relevant.name, path, groups
        )
        logger.info(
            "Кэш школы «%s» обновлён: %s (%d групп)",
            channel.name,
            relevant.name,
            len(groups),
        )

    return {
        "file_id": relevant.id,
        "file_name": relevant.name,
        "file_path": path,
        "groups": groups,
    }


def _render_sheet_group(
    path,
    group: str,
    title: str,
    section: str | None,
    bells: dict | None,
    sat_bells: dict | None,
    target_date: date | None = None,
) -> Image.Image | None:
    _, data = schedule_source.parse_sheet(_read_text(path), section)
    blocks = data.get(group)
    if not blocks:
        return None

    if target_date:
        # Фильтруем блоки по дате
        filtered = []
        for block in blocks:
            b_date = schedule_source.parse_date(block.title)
            if b_date == target_date:
                filtered.append(block)
        blocks = filtered

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
    images_dir: Path | None = None,
    target_date: date | None = None,
) -> bool:
    label = format_date_title(file_name)
    title = group_title(channel, group, file_name)
    if channel.source == "google_sheet":
        image = await asyncio.to_thread(
            _render_sheet_group,
            file_path,
            group,
            title,
            channel.section,
            channel.bells,
            channel.saturday_bells,
            target_date,
        )
        photo_bytes = _to_png_bytes(image) if image is not None else None
    else:
        # Для PDF (МГСТ) target_date уже учтён при выборе файла в get_school_schedule
        photo_bytes = await asyncio.to_thread(_cached_group_bytes, images_dir, group)
        if photo_bytes is None:
            image = await asyncio.to_thread(
                schedule_split.render_group_image, file_path, group, title
            )
            photo_bytes = _to_png_bytes(image) if image is not None else None

    if photo_bytes is None:
        text = f"В расписании нет занятий для группы {group}"
        if target_date:
            text += f" на {target_date.strftime('%d.%m.%Y')}"
        text += "."
        await bot.send_message(chat_id, escape(text), reply_markup=schedule_keyboard(channel.id, group))
        return False

    caption_tail = f" · {label}" if label != channel.name else ""
    caption = escape(f"{header}\n{channel.name} · группа {group}{caption_tail}")
    photo = BufferedInputFile(
        photo_bytes, filename=f"{channel.id.replace(':', '_')}_{group}.png"
    )

    previous_message_id = await asyncio.to_thread(
        storage.get_last_message, chat_id, channel.id
    )
    try:
        message = await bot.send_photo(
            chat_id,
            photo,
            caption=caption[:CAPTION_LIMIT],
            reply_markup=schedule_keyboard(channel.id, group),
        )
    except TelegramAPIError:
        logger.warning("send_photo не сработал, отправляю файлом", exc_info=True)
        message = await bot.send_document(
            chat_id,
            photo,
            caption=caption[:CAPTION_LIMIT],
            reply_markup=schedule_keyboard(channel.id, group),
        )

    await asyncio.to_thread(
        storage.save_last_message, chat_id, channel.id, message.message_id
    )
    await _delete_previous(bot, chat_id, previous_message_id)
    return True


def schedule_keyboard(channel_id: str, group_name: str) -> InlineKeyboardMarkup:
    """Клавиатура с кнопками «На сегодня», «На завтра» и «Сменить класс»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📅 На сегодня", callback_data=f"day:{channel_id}:{group_name}:today"
                ),
                InlineKeyboardButton(
                    text="📅 На завтра", callback_data=f"day:{channel_id}:{group_name}:tomorrow"
                ),
            ],
            [InlineKeyboardButton(text="⚙ Сменить класс", callback_data="back:schools")],
        ]
    )


async def _delete_previous(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        logger.info("Прошлое расписание (message_id=%s) удалить не удалось", message_id)


def _cached_group_bytes(images_dir: Path | None, group: str) -> bytes | None:
    """Готовый PNG группы из кэша: без повторной отрисовки и перекодирования."""
    if not images_dir:
        return None
    path = Path(images_dir) / f"{schedule_split.safe_file_name(group)}.png"
    if not path.exists():
        return None
    with Image.open(path) as image:
        if max(image.size) <= MAX_IMAGE_SIDE and sum(image.size) <= MAX_IMAGE_SUM:
            return path.read_bytes()
        image.load()
        return _to_png_bytes(image)


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
