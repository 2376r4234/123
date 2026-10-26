"""Диалог с пользователем: выбор школы (и смены), группы, подписка, команды."""

from __future__ import annotations

import asyncio
import logging
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import scheduler, storage
from .config import settings

logger = logging.getLogger(__name__)

router = Router()

GROUPS_PER_PAGE = 10

START_TEXT = (
    "Привет! Я присылаю расписание занятий, как только оно появляется на сайте.\n\n"
    "Выберите школу, затем свою группу — и я буду отправлять расписание именно для неё."
)

HELP_TEXT = (
    "Как это работает:\n"
    "1. Нажмите /start и выберите школу.\n"
    "2. Выберите свою группу — подписка оформится автоматически.\n"
    "3. Когда на сайте появится новое расписание, я пришлю фото с занятиями вашей группы.\n\n"
    "Команды:\n"
    "/start — выбрать школу и группу\n"
    "/my — показать текущую подписку\n"
    "/stop — отписаться\n"
    "/admin — панель администратора\n"
    "/help — эта справка"
)


def schools_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=school.name, callback_data=f"sch:{school.id}")]
        for school in settings.schools
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shifts_keyboard(school) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=shift.name, callback_data=f"shift:{school.id}:{shift.id}"
            )
        ]
        for shift in school.shifts
    ]
    rows.append(
        [InlineKeyboardButton(text="📘 Основное расписание", callback_data=f"main:{school.id}")]
    )
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="back:schools")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_shifts_keyboard(school) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=shift.name, callback_data=f"mainshift:{school.id}:{shift.id}"
            )
        ]
        for shift in school.shifts
    ]
    rows.append(
        [InlineKeyboardButton(text="⬅ Назад", callback_data=f"back:shifts:{school.id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_target(channel_id: str) -> str:
    """Куда вернуться из списка групп: к сменам, к основному расписанию или к школам."""
    school_id, _, rest = channel_id.partition(":")
    school = settings.school(school_id)
    if school is None or not school.has_shifts:
        return "back:schools"
    if rest == "main" or rest.endswith(":main"):
        return f"back:main:{school_id}"
    return f"back:shifts:{school_id}"


def groups_keyboard(channel_id: str, groups: list[str], page: int) -> InlineKeyboardMarkup:
    pages = max(1, -(-len(groups) // GROUPS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    start = page * GROUPS_PER_PAGE
    chunk = groups[start : start + GROUPS_PER_PAGE]
    rows = [
        [
            InlineKeyboardButton(
                text=name, callback_data=f"grp:{channel_id}:{start + offset + index}"
            )
            for index, name in enumerate(chunk[offset : offset + 2])
        ]
        for offset in range(0, len(chunk), 2)
    ]
    if pages > 1:
        navigation = []
        if page > 0:
            navigation.append(
                InlineKeyboardButton(text="◀", callback_data=f"gpage:{channel_id}:{page - 1}")
            )
        navigation.append(
            InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop")
        )
        if page < pages - 1:
            navigation.append(
                InlineKeyboardButton(text="▶", callback_data=f"gpage:{channel_id}:{page + 1}")
            )
        rows.append(navigation)
    rows.append(
        [InlineKeyboardButton(text="⬅ Назад", callback_data=_back_target(channel_id))]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _subscriptions_text(user_id: int) -> str:
    subscriptions = await asyncio.to_thread(storage.get_subscriptions, user_id)
    if not subscriptions:
        return "У вас пока нет подписок. Нажмите /start, чтобы выбрать группу."
    lines = []
    for row in subscriptions:
        channel = settings.channel(row["school_id"])
        school_name = channel.name if channel else row["school_id"]
        lines.append(f"• {escape(school_name)} — группа {escape(row['group_name'])}")
    return "Ваши подписки:\n" + "\n".join(lines)


async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        START_TEXT + "\n\n" + await _subscriptions_text(message.from_user.id),
        reply_markup=schools_keyboard(),
    )


async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=schools_keyboard())


async def cmd_my(message: Message) -> None:
    await message.answer(await _subscriptions_text(message.from_user.id))


async def cmd_stop(message: Message, state: FSMContext) -> None:
    await state.clear()
    await asyncio.to_thread(storage.delete_subscription, message.from_user.id)
    await message.answer("Готово, вы отписаны от всех расписаний. Вернуться — /start.")


class AdminStates(StatesGroup):
    waiting_for_broadcast_text = State()


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
                InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin:broadcast"),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Проверить расписание сейчас", callback_data="admin:check"
                )
            ],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin:close")],
        ]
    )


async def cmd_admin(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in settings.admin_ids:
        return
    await state.clear()
    await message.answer("Панель администратора:", reply_markup=admin_keyboard())


async def admin_stats(callback: CallbackQuery) -> None:
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Нет доступа", show_alert=True)
        return
    stats = await asyncio.to_thread(storage.get_stats)
    total_users = await asyncio.to_thread(storage.get_total_users_count)
    lines = ["📊 <b>Статистика бота</b>\n"]
    lines.append(f"Всего пользователей: <b>{total_users}</b>\n")
    lines.append("По школам:")
    if stats:
        for row in stats:
            channel = settings.channel(row["school_id"])
            name = channel.name if channel else row["school_id"]
            lines.append(f"• {escape(name)}: <b>{row['count']}</b> чел.")
    else:
        lines.append("<i>Подписок пока нет.</i>")
    await callback.message.edit_text("\n".join(lines), reply_markup=admin_keyboard())
    await callback.answer()


async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_broadcast_text)
    await callback.message.edit_text(
        "Введите текст для рассылки всем пользователям.\n"
        "Можно использовать HTML-теги (<b>, <i>, <u>).\n\n"
        "Для отмены нажмите кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Отмена", callback_data="admin:cancel_broadcast")]
            ]
        ),
    )


async def admin_cancel_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Рассылка отменена.", reply_markup=admin_keyboard())
    await callback.answer()


async def admin_check_now(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text("Запущена ручная проверка источников…")
    await scheduler.check_all(bot)
    await callback.message.edit_text("Проверка завершена.", reply_markup=admin_keyboard())


async def admin_close(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.delete()


async def process_broadcast(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.from_user.id not in settings.admin_ids:
        return
    broadcast_text = message.text
    if not broadcast_text:
        await message.answer("Пожалуйста, введите текст сообщения.")
        return
    await state.clear()

    users = await asyncio.to_thread(storage.get_all_users)
    status_msg = await message.answer(f"Начинаю рассылку на {len(users)} пользователей…")

    count = 0
    blocked = 0
    errors = 0
    for user_id in users:
        try:
            await bot.send_message(user_id, broadcast_text)
            count += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as retry:
            await asyncio.sleep(retry.retry_after)
            await bot.send_message(user_id, broadcast_text)
            count += 1
        except Exception:
            errors += 1

    await status_msg.edit_text(
        "✅ Рассылка завершена!\n\n"
        f"• Успешно: {count}\n"
        f"• Заблокировали бота: {blocked}\n"
        f"• Ошибок: {errors}",
        reply_markup=admin_keyboard(),
    )


async def _show_groups(callback: CallbackQuery, state: FSMContext, channel) -> None:
    await callback.message.edit_text(f"{escape(channel.name)}: загружаю список групп…")
    try:
        schedule = await scheduler.get_school_schedule(channel)
    except Exception:
        logger.exception("Не удалось получить расписание школы «%s»", channel.name)
        await callback.message.edit_text("Не удалось получить расписание. Попробуйте позже.")
        return
    groups = (schedule or {}).get("groups") or []
    if not groups:
        await callback.message.edit_text(
            "Список групп пока пуст: расписание ещё не опубликовано."
        )
        return
    await state.update_data(channel_id=channel.id, groups=groups)
    await callback.message.edit_text(
        f"{escape(channel.name)}\nВыберите свою группу:",
        reply_markup=groups_keyboard(channel.id, groups, 0),
    )


async def choose_school(callback: CallbackQuery, state: FSMContext) -> None:
    school_id = callback.data.split(":", 1)[1]
    school = settings.school(school_id)
    if school is None:
        await callback.answer("Школа не найдена", show_alert=True)
        return
    await callback.answer()
    if school.has_shifts:
        await callback.message.edit_text(
            f"{escape(school.name)}\nВыберите смену:", reply_markup=shifts_keyboard(school)
        )
        return
    await _show_groups(callback, state, settings.channel(school_id))


async def choose_shift(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Смена не найдена", show_alert=True)
        return
    _, school_id, shift_id = parts
    channel = settings.channel(f"{school_id}:{shift_id}")
    if channel is None:
        await callback.answer("Смена не найдена", show_alert=True)
        return
    await callback.answer()
    await _show_groups(callback, state, channel)


async def choose_main(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «Основное расписание» — постоянное расписание на полугодие."""
    school_id = callback.data.split(":", 1)[1]
    school = settings.school(school_id)
    if school is None:
        await callback.answer("Школа не найдена", show_alert=True)
        return
    await callback.answer()
    if school.has_shifts:
        await callback.message.edit_text(
            f"{escape(school.name)}\n📘 Основное расписание — выберите смену:",
            reply_markup=main_shifts_keyboard(school),
        )
        return
    channel = settings.channel(f"{school_id}:main")
    if channel is None:
        await callback.answer("Школа не найдена", show_alert=True)
        return
    await _show_groups(callback, state, channel)


async def choose_main_shift(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Смена не найдена", show_alert=True)
        return
    _, school_id, shift_id = parts
    channel = settings.channel(f"{school_id}:{shift_id}:main")
    if channel is None:
        await callback.answer("Смена не найдена", show_alert=True)
        return
    await callback.answer()
    await _show_groups(callback, state, channel)


async def paginate_groups(callback: CallbackQuery, state: FSMContext) -> None:
    channel_id, _, page_raw = callback.data[len("gpage:") :].rpartition(":")
    data = await state.get_data()
    groups = data.get("groups") or []
    if not groups or not page_raw.isdigit():
        await callback.answer("Список устарел, начните заново: /start", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_reply_markup(
        reply_markup=groups_keyboard(channel_id, groups, int(page_raw))
    )


async def _send_schedule(bot: Bot, chat_id: int, channel, group: str, header: str) -> None:
    """Отправляет картинку расписания группы (текущего или основного)."""
    try:
        schedule = await scheduler.get_school_schedule(channel)
    except Exception:
        logger.exception("Не удалось получить расписание школы «%s»", channel.name)
        schedule = None

    file_path = (schedule or {}).get("file_path")
    if schedule and file_path and Path(file_path).exists():
        await scheduler.send_group_photo(
            bot,
            chat_id,
            channel,
            file_path,
            group,
            schedule["file_name"],
            header=header,
        )
        return
    await bot.send_message(
        chat_id, "Расписание ещё не опубликовано — пришлю его, как только оно появится."
    )


async def choose_group(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    channel_id, _, index_raw = callback.data[len("grp:") :].rpartition(":")
    data = await state.get_data()
    groups = data.get("groups") or []
    if (
        data.get("channel_id") != channel_id
        or not index_raw.isdigit()
        or int(index_raw) >= len(groups)
    ):
        await callback.answer("Список устарел, начните заново: /start", show_alert=True)
        return
    channel = settings.channel(channel_id)
    if channel is None:
        await callback.answer("Школа не найдена", show_alert=True)
        return

    group = groups[int(index_raw)]
    chat_id = callback.message.chat.id

    if channel.section == "main":
        await callback.answer()
        await callback.message.edit_text(
            f"{escape(channel.name)}\nГруппа {escape(group)}. Отправляю расписание…"
        )
        await _send_schedule(bot, chat_id, channel, group, header="Основное расписание")
        return

    await callback.answer("Подписка оформлена")
    await asyncio.to_thread(storage.set_subscription, callback.from_user.id, channel_id, group)
    await state.clear()
    await callback.message.edit_text(
        f"Готово! Вы подписаны на расписание группы {escape(group)} ({escape(channel.name)}).\n"
        "Отправляю текущее расписание…"
    )
    await _send_schedule(bot, chat_id, channel, group, header="Текущее расписание")


async def go_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат на предыдущий шаг: к списку школ, к сменам или к основному расписанию."""
    target = callback.data[len("back:") :]
    await callback.answer()
    await state.clear()
    if target == "schools":
        await callback.message.edit_text("Выберите школу:", reply_markup=schools_keyboard())
        return

    kind, _, school_id = target.partition(":")
    school = settings.school(school_id)
    if school is None:
        await callback.message.edit_text("Выберите школу:", reply_markup=schools_keyboard())
        return
    if kind == "main":
        await callback.message.edit_text(
            f"{escape(school.name)}\n📘 Основное расписание — выберите смену:",
            reply_markup=main_shifts_keyboard(school),
        )
        return
    await callback.message.edit_text(
        f"{escape(school.name)}\nВыберите смену:", reply_markup=shifts_keyboard(school)
    )


async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


async def fallback(message: Message) -> None:
    await message.answer("Нажмите /start, чтобы выбрать школу и группу.")


router.message(CommandStart())(cmd_start)
router.message(Command("help"))(cmd_help)
router.message(Command("my"))(cmd_my)
router.message(Command("stop"))(cmd_stop)
router.message(Command("admin"))(cmd_admin)

router.callback_query(F.data == "admin:stats")(admin_stats)
router.callback_query(F.data == "admin:broadcast")(admin_broadcast_start)
router.callback_query(F.data == "admin:cancel_broadcast")(admin_cancel_broadcast)
router.callback_query(F.data == "admin:check")(admin_check_now)
router.callback_query(F.data == "admin:close")(admin_close)

router.message(AdminStates.waiting_for_broadcast_text)(process_broadcast)

router.callback_query(F.data.startswith("sch:"))(choose_school)
router.callback_query(F.data.startswith("shift:"))(choose_shift)
router.callback_query(F.data.startswith("mainshift:"))(choose_main_shift)
router.callback_query(F.data.startswith("main:"))(choose_main)
router.callback_query(F.data.startswith("gpage:"))(paginate_groups)
router.callback_query(F.data.startswith("grp:"))(choose_group)
router.callback_query(F.data.startswith("back:"))(go_back)
router.callback_query(F.data == "noop")(noop)

router.message()(fallback)
