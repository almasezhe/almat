from __future__ import annotations
from html import escape
import os
import asyncio
import logging
from secrets import token_hex
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from uuid import UUID

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from database import supabase
from .assign_lead import (
    create_and_assign_lead,
    preview_category_queue,
)
from .events import log_event


router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)

LOCAL_TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Almaty"))


class AddLeadState(StatesGroup):
    waiting_for_link = State()


class ChangeLeadState(StatesGroup):
    choosing_lead = State()
    confirming_lead = State()
    choosing_new_category = State()

def get_chat_ids() -> list[int]:
    result = (
        supabase.table("chats")
        .select("chat_id")
        .execute()
    )
    return [
        int(row["chat_id"])
        for row in (result.data or [])
        if row.get("chat_id") is not None
    ]


def get_member_by_telegram_id(telegram_id: int) -> dict | None:
    result = (
        supabase.table("team_members")
        .select(
            "id, telegram_id, telegram_username, display_name, "
            "is_admin, can_receive_leads, status, is_active"
        )
        .eq("telegram_id", telegram_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def categories_keyboard(prefix: str, *, exclude_id: str | None = None) -> InlineKeyboardMarkup:
    result = (
        supabase.table("categories")
        .select("id, name")
        .eq("is_active", True)
        .order("sort_order")
        .execute()
    )
    rows = []
    for category in result.data or []:
        if exclude_id and category["id"] == exclude_id:
            continue
        if not rows or len(rows[-1]) == 3:
            rows.append([])
        
        rows[-1].append(
            InlineKeyboardButton(
                text=category["name"],
                callback_data=f"{prefix}:{category['id']}",
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def deny_if_not_member(message: Message) -> dict | None:
    member = get_member_by_telegram_id(message.from_user.id)
    if not member or not member["is_active"]:
        await message.answer("Вы не зарегистрированы как активный менеджер. Пожалуйста, используйте /start для регистрации.")
        return None
    return member




async def notify_team(bot, text: str, *, parse_mode=None) -> None:
    for chat_id in get_chat_ids():
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except TelegramAPIError:
            pass

# ============================================================
# START / ACCESS CONTROL
# ============================================================
@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Текущее действие отменено.")


@router.message(CommandStart())
async def start_handler(message: Message):
    user = message.from_user
    member = get_member_by_telegram_id(user.id)
    joining = member is None or not member["is_active"]
    display_name = user.first_name or (member or {}).get("display_name") or str(user.id)

    if member is None:
        result = (
            supabase.table("team_members")
            .insert({
                "telegram_id": user.id,
                "telegram_username": user.username,
                "display_name": display_name,
                "is_admin": False,
                "is_active": True,
                "can_receive_leads": True,
                "status": "OFFLINE",
            })
            .execute()
        )
        member = result.data[0]
    else:
        updates = {
            "telegram_username": user.username,
            "display_name": display_name,
        }
        if joining:
            updates.update(is_active=True, can_receive_leads=True, status="OFFLINE")
        (
            supabase.table("team_members")
            .update(updates)
            .eq("id", member["id"])
            .execute()
        )

    # Registration must include queue membership to actually receive leads.
    existing_queues = (
        supabase.table("category_queue_members")
        .select("category_id, enabled")
        .eq("member_id", member["id"])
        .execute()
    )
    queues_by_category = {
        row["category_id"]: row for row in (existing_queues.data or [])
    }
    categories = (
        supabase.table("categories")
        .select("id, code")
        .eq("is_active", True)
        .eq("is_distributable", True)
        .execute()
    )
    for category in categories.data or []:
        if category["code"] == "trash":
            continue
        queue = queues_by_category.get(category["id"])
        if queue is not None:
            if joining and not queue["enabled"]:
                (
                    supabase.table("category_queue_members")
                    .update({"enabled": True})
                    .eq("category_id", category["id"])
                    .eq("member_id", member["id"])
                    .execute()
                )
            continue  # Repeated /start never moves an existing queue position.

        last = (
            supabase.table("category_queue_members")
            .select("position")
            .eq("category_id", category["id"])
            .order("position", desc=True)
            .limit(1)
            .execute()
        )
        position = last.data[0]["position"] + 1 if last.data else 1
        (
            supabase.table("category_queue_members")
            .insert({
                "category_id": category["id"],
                "member_id": member["id"],
                "position": position,
                "enabled": True,
            })
            .execute()
        )

    if joining:
        log_event(
            "MEMBER_ADD",
            actor_id=member["id"],
            member_id=member["id"],
            actor_name_snapshot=display_name,
            member_name_snapshot=display_name,
            metadata={"telegram_id": user.id, "self_registration": True},
        )

    await message.answer(
        ("Вы присоединились к команде! Выберите /status → ONLINE, чтобы получать лиды.\n" if joining else "Бот подключен.\n")
        + "Используйте /send для добавления лида, /status для изменения статуса, "
        "и /my_leads для просмотра ваших лидов."
    )


# ============================================================
# STATUS
# ============================================================
@router.message(Command("status"))
async def status_handler(message: Message):
    member = await deny_if_not_member(message)
    if not member:
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="ONLINE", callback_data="set_status:ONLINE"),
                InlineKeyboardButton(text="OFFLINE", callback_data="set_status:OFFLINE"),
            ]
        ]
    )

    await message.answer(
        f"Нынешний статус: {member['status']}\nВыберите новый:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("set_status:"))
async def status_callback(callback: CallbackQuery):
    member = get_member_by_telegram_id(callback.from_user.id)
    if not member or not member["is_active"]:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    new_status = callback.data.split(":", 1)[1]
    if new_status not in {"ONLINE", "OFFLINE"}:
        await callback.answer("Неверный статус.", show_alert=True)
        return

    old_status = str(member["status"])
    if old_status == new_status:
        await callback.answer(f"Уже {new_status}.")
        await callback.message.edit_reply_markup(reply_markup=None)
        return

    (
        supabase.table("team_members")
        .update({"status": new_status})
        .eq("id", member["id"])
        .execute()
    )

    log_event(
        "STATUS_CHANGE",
        actor_id=member["id"],
        member_id=member["id"],
        old_status=old_status,
        new_status=new_status,
        actor_name_snapshot=member["display_name"],
        member_name_snapshot=member["display_name"],
    )

    team_text = (
        f"{member['display_name']} ОНЛАЙН 😈"
        if new_status == "ONLINE"
        else f"{member['display_name']} ОФФЛАЙН 😴"
    )
    await notify_team(callback.bot, team_text)

    await callback.message.edit_text(f"Status changed: {old_status} → {new_status}")
    await callback.answer()


# Backwards-compatible old command.
@router.message(Command("change_status"))
async def old_change_status_handler(message: Message):
    await status_handler(message)


# ============================================================
# RESET STATUS (ADMIN)
# ============================================================
@router.message(Command("reset_status", "reset_all_status"))
async def reset_status_handler(message: Message):
    member = await deny_if_not_member(message)
    if not member:
        return
    if not member["is_admin"]:
        await message.answer("У вас нет прав для выполнения этого действия.")
        return

    (
        supabase.table("team_members")
        .update({"status": "OFFLINE"})
        .eq("is_active", True)
        .execute()
    )

    log_event(
        "RESET_STATUS",
        actor_id=member["id"],
        actor_name_snapshot=member["display_name"],
        metadata={"new_status": "OFFLINE"},
    )

    await message.answer("Все статусы менеджеров были сброшены на OFFLINE.")
    await notify_team(message.bot, "Всем спатьки 😴. Статусы менеджеров сброшены на OFFLINE.")


# ============================================================
# SEND LEAD
# ============================================================
@router.message(Command("send", "add_lead"))
async def add_lead_handler(message: Message, state: FSMContext):
    member = await deny_if_not_member(message)
    if not member:
        return

    await state.clear()
    keyboard = categories_keyboard("send_category")
    if not keyboard.inline_keyboard:
        await message.answer("Нету активных категорий для добавления лидов.")
        return

    await message.answer("Выберите категорию:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("send_category:"))
async def send_category_handler(callback: CallbackQuery, state: FSMContext):
    member = get_member_by_telegram_id(callback.from_user.id)
    if not member or not member["is_active"]:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    category_id = callback.data.split(":", 1)[1]
    await state.update_data(current_category_id=category_id)
    await state.set_state(AddLeadState.waiting_for_link)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Отправьте ссылку:")
    await callback.answer()


@router.message(AddLeadState.waiting_for_link, ~F.text.startswith("/"))
async def lead_link_handler(message: Message, state: FSMContext):
    member = await deny_if_not_member(message)
    if not member:
        await state.clear()
        return

    if not message.text:
        await message.answer("Отправьте действительную ссылку.")
        return

    lead_url = message.text.strip()
    if not lead_url.startswith(("http://", "https://")):
        await message.answer("Отправьте действительную ссылку, начинающуюся с http:// или https://")
        return

    state_data = await state.get_data()
    category_id = state_data.get("current_category_id")
    if not category_id:
        await state.clear()
        await message.answer("Сессия добавления лида истекла. Пожалуйста, используйте /send снова.")
        return

    result = create_and_assign_lead(
        lead_url=lead_url,
        category_id=category_id,
        submitter_id=member["id"],
        submitter_username=message.from_user.username,
    )
    await state.clear()

    status = result.get("status")
    lead_id = result.get("lead_id")
    lead_title = f"Лид #{lead_id}" if lead_id is not None else "Неизвестный лид"

    if status == "duplicate":
        assignee = result.get("assignee_name") or "Не распределен"
        created_at = result.get("created_at") or "Не известно"
        await message.answer(
            f"{lead_title} уже был добавлен.\n\n"
            f"Категория: {result.get('category_name', 'Не известно')}\n"
            f"Ответственный: {assignee}\n"
            f"Создан: {created_at}",
            parse_mode=None,
        )
        return

    if status == "trash":
        await message.answer(f"{lead_title} saved as trash.")
        return

    if status == "category_not_distributable":
        await message.answer("Эта категория не распределяема")
        return

    if status in {"category_not_found", "submitter_not_found", "invalid_url"}:
        await message.answer(f"Не получилось добавить лид: {status}")
        return

    if status == "unassigned":
        await message.answer(
            f"{lead_title} сохранен и не распределен. В этой категории нет доступных менеджеров.",
        )
        return

    if status != "assigned":
        await message.answer(f"{lead_title}: unexpected distribution result: {status}", parse_mode=None)
        return

    assignee_name = result.get("assignee_name") or "Не известно"
    assignee_telegram_id = result.get("assignee_telegram_id")
    category_name = result.get("category_name") or "Не известно"

    delivery_failed = False
    if assignee_telegram_id:
        try:
            await message.bot.send_message(
                chat_id=int(assignee_telegram_id),
                text=(
                    "У вас новый лид\n\n"
                    f"{lead_title}\n"
                    f"Категория: {category_name}\n\n"
                    f"Ссылка: {lead_url}"
                ),
                parse_mode=None,
            )
        except TelegramAPIError as exc:
            delivery_failed = True
            log_event(
                "DELIVERY_ERROR",
                actor_id=member["id"],
                member_id=result.get("assignee_id"),
                lead_id=result.get("lead_id"),
                category_id=category_id,
                actor_name_snapshot=member["display_name"],
                member_name_snapshot=assignee_name,
                category_name_snapshot=category_name,
                metadata={"error": str(exc)},
            )

    
    if delivery_failed:
        await message.answer(
        f"{lead_title} assigned → {assignee_name}\n"
        "Warning: Telegram DM delivery failed. The assignment is still saved.",
        parse_mode=None,
        )
    else:
        await message.answer(
            f"{lead_title} успешно назначен → {assignee_name}",
            parse_mode=None,
        )

        await notify_team(
            message.bot,
            (
                f"{lead_title} распределен\n\n"
                f"Менеджер: {escape(assignee_name)}\n"
                f"Категория: {escape(category_name)}\n"
                f"Отправитель: {escape(message.from_user.first_name)}\n\n"

                f"Ссылка: {lead_url}"
            ),
            parse_mode=None,
            )


# ============================================================
# CHANGE CATEGORY
# ============================================================

async def show_change_leads(message, state, member, *, edit=False):
    """Display the latest five owned leads in a fresh, message-bound session."""
    await state.clear()
    leads = (
        supabase.table("leads")
        .select("id, lead_url, current_category_id, created_at")
        .eq("assignee_id", member["id"])
        .order("created_at", desc=True)
        .order("id", desc=True)
        .limit(5)
        .execute()
    ).data or []
    categories = []
    if leads:
        categories = (
            supabase.table("categories")
            .select("id, name")
            .in_("id", list({lead["current_category_id"] for lead in leads}))
            .execute()
        ).data or []
    names = {category["id"]: category["name"] for category in categories}
    session = token_hex(4)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=(
                f"#{lead['id']} · {names.get(lead['current_category_id'], 'Не известно')} "
            ),
            callback_data=f"change_lead:{session}:{lead['id']}",
        )]
        for lead in leads
    ])
    await state.update_data(change_session=session)
    await state.set_state(ChangeLeadState.choosing_lead)
    text = (
        "Выберите одного из последних 5 лидов или отправьте ID любого своего лида "
        "(например, 123 или #123).\n"
        if leads else
        "У вас пока нет лидов. Можно отправить ID для проверки.\n"
    )
    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode=None)
        message_id = message.message_id
    else:
        sent = await message.answer(text, reply_markup=keyboard, parse_mode=None)
        message_id = sent.message_id
    await state.update_data(change_message_id=message_id)


async def change_session_data(callback, state, expected_state):
    data = await state.get_data()
    parts = (callback.data or "").split(":", 2)
    if (
        len(parts) != 3
        or parts[1] != data.get("change_session")
        or await state.get_state() != expected_state.state
        or callback.message is None
        or callback.message.message_id != data.get("change_message_id")
    ):
        await callback.answer("Время выбора истекло. Используйте /change снова.", show_alert=True)
        return None
    return data


def get_owned_change_lead(lead_id, member_id):
    result = (
        supabase.table("leads")
        .select("id, lead_url, current_category_id, assignee_id")
        .eq("id", lead_id)
        .eq("assignee_id", member_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def change_category_name(category_id):
    result = (
        supabase.table("categories")
        .select("name")
        .eq("id", category_id)
        .limit(1)
        .execute()
    )
    return result.data[0]["name"] if result.data else "Unknown"


def parse_change_lead_id(text):
    value = (text or "").strip()
    if value.startswith("#"):
        value = value[1:]
    if not value or len(value) > 19 or not value.isascii() or not value.isdecimal():
        return None
    number = int(value)
    return number if 0 < number <= 9223372036854775807 else None


async def show_change_confirmation(message, state, lead, *, edit=False):
    # A new token invalidates all previously displayed buttons.
    session = token_hex(4)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да", callback_data=f"change_confirm:{session}:yes"),
        InlineKeyboardButton(text="Нет", callback_data=f"change_confirm:{session}:no"),
    ]])
    text = (
        f"Сменить категорию лида #{lead['id']}\n\n"
        f"Текущая категория: {change_category_name(lead['current_category_id'])}\n\n"
        f"Ссылка: {lead['lead_url']}"
    )
    if edit:
        await message.edit_text(text, reply_markup=keyboard, parse_mode=None)
        message_id = message.message_id
        
    else:
        sent = await message.answer(text, reply_markup=keyboard, parse_mode=None)
        message_id = sent.message_id
    await state.clear()
    await state.update_data(
        change_session=session, change_message_id=message_id, lead_id=lead["id"]
    )
    await state.set_state(ChangeLeadState.confirming_lead)


async def select_change_lead_by_text(message, state, member, text):
    lead_id = parse_change_lead_id(text)
    if lead_id is None:
        await message.answer("Отправьте положительный ID лида: 123 или #123.")
        return
    lead = get_owned_change_lead(lead_id, member["id"])
    if not lead:
        await message.answer(f"Лид #{lead_id} не найден или не принадлежит вам.")
        return
    await show_change_confirmation(message, state, lead)


@router.message(Command("change"))
async def change_handler(message: Message, state: FSMContext):
    member = await deny_if_not_member(message)
    if not member:
        await state.clear()
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) == 1:
        await show_change_leads(message, state, member)
        return
    await state.clear()
    await state.set_state(ChangeLeadState.choosing_lead)
    await select_change_lead_by_text(message, state, member, args[1])


@router.message(ChangeLeadState.choosing_lead, ~F.text.startswith("/"))
async def change_lead_id_handler(message: Message, state: FSMContext):
    member = await deny_if_not_member(message)
    if not member:
        await state.clear()
        return
    await select_change_lead_by_text(message, state, member, message.text)


@router.callback_query(F.data.startswith("change_lead:"))
async def change_lead_select_handler(callback: CallbackQuery, state: FSMContext):
    member = get_member_by_telegram_id(callback.from_user.id)
    if not member or not member["is_active"]:
        await callback.answer("Access denied.", show_alert=True)
        return
    data = await change_session_data(callback, state, ChangeLeadState.choosing_lead)
    if data is None:
        return
    lead_id = parse_change_lead_id(callback.data.split(":", 2)[2])
    if lead_id is None:
        await callback.answer("Неверный лид.", show_alert=True)
        return
    lead = get_owned_change_lead(lead_id, member["id"])
    if not lead:
        await callback.answer(f"Лид #{lead_id} не найден или не принадлежит вам.", show_alert=True)
        return
    await show_change_confirmation(callback.message, state, lead, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("change_confirm:"))
async def change_lead_confirm_handler(callback: CallbackQuery, state: FSMContext):
    member = get_member_by_telegram_id(callback.from_user.id)
    if not member or not member["is_active"]:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    data = await change_session_data(callback, state, ChangeLeadState.confirming_lead)
    if data is None:
        return
    decision = callback.data.split(":", 2)[2]
    if decision == "no":
        await show_change_leads(callback.message, state, member, edit=True)
        await callback.answer()
        return
    if decision != "yes":
        await callback.answer("Неверный выбор.", show_alert=True)
        return
    lead = get_owned_change_lead(data.get("lead_id"), member["id"])
    if not lead:
        await show_change_leads(callback.message, state, member, edit=True)
        await callback.answer(f"Лид #{data['lead_id']} не найден или не принадлежит вам.", show_alert=True)
        return
    # Refresh the actual current category only after confirmation.
    await state.update_data(
        lead_id=lead["id"], lead_url=lead["lead_url"], old_category_id=lead["current_category_id"]
    )
    keyboard = categories_keyboard(
        f"change_new:{data['change_session']}", exclude_id=lead["current_category_id"]
    )
    if not keyboard.inline_keyboard:
        await show_change_leads(callback.message, state, member, edit=True)
        await callback.answer("Нету активных доступных категорий", show_alert=True)
        return
    await state.set_state(ChangeLeadState.choosing_new_category)
    await callback.message.edit_text(
        f"Лид #{lead['id']}\n"
        f"Нынешняя категория: {change_category_name(lead['current_category_id'])}\n\n"
        "Выберите другую категорию:",
        reply_markup=keyboard,
        parse_mode=None,
    )
    await callback.answer()


def format_category_change(lead_id, lead_url: str, member: dict, result: dict) -> str:
    manager = escape(str(result.get("assignee_name") or member["display_name"]))
    old_name = escape(str(result.get("old_category_name") or "Неизвестно"))
    new_name = escape(str(result.get("new_category_name") or "Неизвестно"))
    blocks = [
        f"<b>Смена Категории - лид #{escape(str(lead_id))}</b>\n"
        f"Менеджер: {manager}",
        f"Ссылка: {escape(lead_url)}",
        f"<b>Категория успешно изменена:</b>\n{old_name} → {new_name}",
    ]
    if result.get("compensation_created"):
        blocks.append(
            f"<b>Изменения в компенсации:</b>\n"
            f"{old_name}: компенсация для {manager}"
        )
    if result.get("skip_created"):
        blocks.append(
            f"<b>Изменения в пропусках:</b>\n"
            f"{new_name}: {manager} пропускает следующего лида"
        )
    return "\n\n".join(blocks)


@router.callback_query(
    F.data.startswith("change_new:")
)
async def change_new_category_handler(
    callback: CallbackQuery,
    state: FSMContext
):
    member = get_member_by_telegram_id(
        callback.from_user.id
    )

    if not member or not member["is_active"]:
        await callback.answer(
            "Доступ запрещен.",
            show_alert=True
        )
        return

    data = await change_session_data(callback, state, ChangeLeadState.choosing_new_category)
    if data is None:
        return
    new_category_id = callback.data.split(":", 2)[2]

    lead_id = data.get("lead_id")
    lead_url = data.get("lead_url")
    old_category_id = data.get(
        "old_category_id"
    )

    if (
        not lead_id
        or not lead_url
        or not old_category_id
    ):
        await state.clear()

        await callback.message.answer(
            (f"Лид #{lead_id}: " if lead_id is not None else "")
            + "Сессия изменения истекла. Используйте /change снова."
        )

        await callback.answer()
        return

    lead = get_owned_change_lead(lead_id, member["id"])
    if not lead:
        await state.clear()
        await callback.answer(
            f"Лид #{lead_id} не найден или больше не принадлежит вам.", show_alert=True
        )
        return
    if str(lead["current_category_id"]) != str(old_category_id):
        await state.clear()
        await callback.answer("Категория уже изменена. Используйте /change снова.", show_alert=True)
        return
    try:
        new_category_id = str(UUID(new_category_id))
    except (ValueError, TypeError, AttributeError):
        await callback.answer("Неверная категория.", show_alert=True)
        return
    lead_url = lead["lead_url"]
    # The RPC checks ownership again while holding a row lock.
    try:
        result = supabase.rpc("change_owned_lead_category_atomic", {
            "p_lead_id": lead_id,
            "p_old_category_id": old_category_id,
            "p_new_category_id": new_category_id,
            "p_actor_telegram_id": callback.from_user.id,
        }).execute().data
    except Exception:
        await state.clear()
        await callback.message.answer(
            f"Лид #{lead_id}: не удалось подтвердить результат операции. ",
            parse_mode=None,
        )
        await callback.answer()
        return

    await state.clear()

    status = result.get("status")

    if status == "changed":
        text = format_category_change(lead_id, lead_url, member, result)

        await callback.answer()
        # Same success message goes to every registered team group, even if
        # the private message can no longer be edited.
        try:
            await callback.message.edit_text(text, parse_mode="HTML")
        finally:
            await notify_team(callback.bot, text, parse_mode="HTML")
        return

    errors = {
        "lead_not_found":
            "Лид не найден или больше не принадлежит вам.",

        "old_category_mismatch":
            "Категория лида уже изменена.",

        "same_category":
            "Это и есть текущая категория. Выберите другую.",

        "new_category_not_distributable":
            "Эта категория в данный момент недоступна для распределения.",

        "new_category_not_found":
            "Категория не найдена.",

        "old_category_not_found":
            "Старая категория не найдена.",

        "actor_not_found":
            "Ваша запись участника команды не найдена.",
    }

    await callback.message.answer(
        f"Lead #{lead_id}\n\n" + errors.get(
            status,
            f"Не получилось изменить категорию: {status}"
        ),
        parse_mode=None,
    )

    await callback.answer()
# ============================================================
# QUEUES (ALL ACTIVE MEMBERS)
# ============================================================
@router.message(Command("queues"))
async def queues_handler(message: Message):
    member = await deny_if_not_member(message)
    if not member:
        return

    categories = (
        supabase.table("categories")
        .select("id, name, code")
        .eq("is_active", True)
        .eq("is_distributable", True)
        .order("sort_order")
        .execute()
    )

    blocks = []
    for category in categories.data or []:
        if category["code"] == "trash":
            continue
        try:
            preview = await asyncio.to_thread(preview_category_queue, category["id"], 5)
        except Exception:
            logging.exception("Queue preview failed for category %s", category["id"])
            await message.answer(
                "Не удалось получить очередь. Администратору: проверьте, что "
                "queues_preview.sql выполнен в Supabase.",
                parse_mode=None,
            )
            return
        next_text = " → ".join(
            entry["display_name"] for entry in preview.get("next_members", [])
        ) or "Нет доступных менеджеров онлайн"
        compensations = ", ".join(preview.get("compensations", [])) or "нет"
        skips = ", ".join(preview.get("skips", [])) or "нет"
        blocks.append(
            f"{category['name']}\n"
            f"Следующие: {next_text}\n"
            f"Компенсации: {compensations}\n"
            f"Пропуски: {skips}"
        )

    await answer_plain_chunks(
        message, "\n\n".join(blocks) if blocks else "Нет категорий для распределения."
    )


# ============================================================
# MY LEADS
# ============================================================
def format_lead_timestamp(value: str | None) -> str:
    if not value:
        return "unknown date"
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "unknown date"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(LOCAL_TZ).strftime("%H:%M:%S")


def today_assignment_bounds(now: datetime | None = None) -> tuple[str, str, str]:
    local_now = now.astimezone(LOCAL_TZ) if now is not None else datetime.now(LOCAL_TZ)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return (
        start.astimezone(timezone.utc).isoformat(),
        end.astimezone(timezone.utc).isoformat(),
        start.strftime("%d.%m.%Y"),
    )


def get_today_leads(member_id: str, start: str, end: str) -> list[dict]:
    leads = []
    # Stable pagination avoids the API's default row cap.
    while True:
        page = (
            supabase.table("leads")
            .select(
                "id, lead_url, original_category_id, current_category_id, "
                "assigned_at, created_at"
            )
            .eq("assignee_id", member_id)
            .gte("assigned_at", start)
            .lt("assigned_at", end)
            .order("assigned_at", desc=True)
            .order("id", desc=True)
            .range(len(leads), len(leads) + 499)
            .execute()
        ).data or []
        if not page:
            return leads
        leads.extend(page)


def split_plain_text(text: str, limit: int = 4000) -> list[str]:
    # Budget UTF-16 units too, so names with emoji cannot exceed Telegram limits.
    chunks = []
    while text:
        units = 0
        cut = len(text)
        for index, char in enumerate(text):
            units += 2 if ord(char) > 0xFFFF else 1
            if units > limit:
                cut = index
                break
        if cut < len(text):
            newline = text.rfind("\n", 0, cut)
            if newline > 0:
                cut = newline + 1
        chunks.append(text[:cut])
        text = text[cut:]
    return chunks


async def answer_plain_chunks(message: Message, text: str) -> None:
    for chunk in split_plain_text(text):
        await message.answer(chunk, parse_mode=None)


@router.message(Command("myleads", "my_leads"))
async def my_leads_handler(message: Message):
    member = await deny_if_not_member(message)
    if not member:
        return

    start, end, date_label = today_assignment_bounds()
    leads = await asyncio.to_thread(get_today_leads, member["id"], start, end)
    if not leads:
        await message.answer(f"Сегодня ({date_label}) у вас нет назначенных лидов.")
        return

    category_ids = {
        lead[key] for lead in leads
        for key in ("current_category_id", "original_category_id")
        if lead.get(key) is not None
    }
    categories_result = (
        supabase.table("categories")
        .select("id, name")
        .in_("id", list(category_ids))
        .execute()
    )
    category_names = {
        row["id"]: row["name"] for row in (categories_result.data or [])
    }
    counts = Counter(
        category_names.get(lead["current_category_id"], "Неизвестно")
        for lead in leads
    )
    summary_lines = [f"{name} — {count}" for name, count in counts.most_common()]
    lead_lines = []
    for lead in leads:
        current_name = category_names.get(lead["current_category_id"], "Неизвестно")
        original_name = category_names.get(lead["original_category_id"], "Неизвестно")
        category_text = current_name
        if original_name != current_name:
            category_text += f" (изначально: {original_name})"
        assigned = format_lead_timestamp(lead.get("assigned_at"))
        lead_lines.append(
            f"• Лид #{lead['id']} · {category_text}\n  {lead['lead_url']}\n  {assigned}"
        )
    text = (
        f"{member['display_name']}\nСегодня: {date_label}\n\n"
        + "\n".join(summary_lines)
        + f"\n\nВсего за сегодня — {len(leads)}"
        + "\n\nЛиды за сегодня:\n"
        + "\n\n".join(lead_lines)
    )
    await answer_plain_chunks(message, text)


# ============================================================
# STATS
# ============================================================
@router.message(Command("stats"))
async def stats_handler(message: Message):
    member = await deny_if_not_member(message)
    if not member:
        return

    local_now = datetime.now(LOCAL_TZ)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_midnight = local_midnight.astimezone(timezone.utc).isoformat()

    leads_result = (
        supabase.table("leads")
        .select("id, current_category_id, assignee_id, created_at")
        .gte("created_at", utc_midnight)
        .execute()
    )
    leads = leads_result.data or []

    categories_result = (
        supabase.table("categories")
        .select("id, name")
        .execute()
    )
    category_names = {
        row["id"]: row["name"] for row in (categories_result.data or [])
    }

    members_result = (
        supabase.table("team_members")
        .select("id, display_name")
        .execute()
    )
    member_names = {
        row["id"]: row["display_name"] for row in (members_result.data or [])
    }

    category_counts = Counter(
        category_names.get(row["current_category_id"], "Unknown")
        for row in leads
    )
    member_counts = Counter(
        member_names.get(row["assignee_id"], "Unassigned")
        for row in leads
    )

    changes_result = (
        supabase.table("events")
        .select("old_category_name_snapshot, new_category_name_snapshot")
        .eq("event_type", "CATEGORY_CHANGE")
        .gte("created_at", utc_midnight)
        .execute()
    )
    changes = changes_result.data or []
    change_pairs = Counter(
        f"{row.get('old_category_name_snapshot') or '?'} → "
        f"{row.get('new_category_name_snapshot') or '?'}"
        for row in changes
    )

    category_lines = [f"{name} — {count}" for name, count in category_counts.most_common()]
    member_lines = [f"{name} — {count}" for name, count in member_counts.most_common()]
    change_lines = [f"{name} — {count}" for name, count in change_pairs.most_common()]

    text = (
        "За сегодня:\n\n"
        + ("\n".join(category_lines) or "Нету лидов")
        + f"\n\nTotal — {len(leads)}\n\n"
        + "По менеджерам:\n"
        + ("\n".join(member_lines) or "Нету назначений")
        + f"\n\nИзменения категорий — {len(changes)}"
    )
    if change_lines:
        text += "\n" + "\n".join(change_lines)

    await message.answer(text[:4096], parse_mode=None)


# ============================================================
# ADMIN: TEAM MEMBERS
# ============================================================
@router.message(Command("add_member"))
async def add_member_handler(message: Message):
    admin = await deny_if_not_member(message)
    if not admin:
        return
    if not admin["is_admin"]:
        await message.answer("Admin only.")
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: /add_member <telegram_id> <display name>")
        return

    try:
        telegram_id = int(parts[1])
    except ValueError:
        await message.answer("telegram_id должен быть числом.")
        return

    display_name = parts[2].strip()
    existing = get_member_by_telegram_id(telegram_id)

    if existing:
        (
            supabase.table("team_members")
            .update(
                {
                    "display_name": display_name,
                    "is_active": True,
                    "can_receive_leads": True,
                    "status": "OFFLINE",
                }
            )
            .eq("id", existing["id"])
            .execute()
        )
        member_id = existing["id"]
    else:
        inserted = (
            supabase.table("team_members")
            .insert(
                {
                    "telegram_id": telegram_id,
                    "display_name": display_name,
                    "is_admin": False,
                    "can_receive_leads": True,
                    "status": "OFFLINE",
                    "is_active": True,
                }
            )
            .execute()
        )
        member_id = inserted.data[0]["id"]


    categories = (
        supabase.table("categories")
        .select("id, code, is_distributable")
        .eq("is_active", True)
        .eq("is_distributable", True)
        .execute()
    )
    for category in categories.data or []:
        if category["code"] == "trash":
            continue
        positions = (
            supabase.table("category_queue_members")
            .select("position")
            .eq("category_id", category["id"])
            .order("position", desc=True)
            .limit(1)
            .execute()
        )
        next_position = (positions.data[0]["position"] + 1) if positions.data else 1
        (
            supabase.table("category_queue_members")
            .upsert(
                {
                    "category_id": category["id"],
                    "member_id": member_id,
                    "position": next_position,
                    "enabled": True,
                },
                on_conflict="category_id,member_id",
            )
            .execute()
        )

    log_event(
        "MEMBER_ADD",
        actor_id=admin["id"],
        member_id=member_id,
        actor_name_snapshot=admin["display_name"],
        member_name_snapshot=display_name,
        metadata={"telegram_id": telegram_id},
    )
    await message.answer(f"Мембер добавлен/включен: {display_name}")


@router.message(Command("remove_member"))
async def remove_member_handler(message: Message):
    admin = await deny_if_not_member(message)
    if not admin:
        return
    if not admin["is_admin"]:
        await message.answer("Admin only.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Usage: /remove_member <telegram_id>")
        return
    try:
        telegram_id = int(parts[1])
    except ValueError:
        await message.answer("telegram_id must be an integer.")
        return

    target = get_member_by_telegram_id(telegram_id)
    if not target:
        await message.answer("Мембер не найден")
        return

    (
        supabase.table("team_members")
        .update(
            {
                "is_active": False,
                "can_receive_leads": False,
                "status": "OFFLINE",
            }
        )
        .eq("id", target["id"])
        .execute()
    )
    (
        supabase.table("category_queue_members")
        .update({"enabled": False})
        .eq("member_id", target["id"])
        .execute()
    )

    log_event(
        "MEMBER_REMOVE",
        actor_id=admin["id"],
        member_id=target["id"],
        actor_name_snapshot=admin["display_name"],
        member_name_snapshot=target["display_name"],
    )
    await message.answer(f"Мембер отключен: {target['display_name']}")


@router.message(Command("allow_leads", "block_leads"))
async def lead_permission_handler(message: Message):
    admin = await deny_if_not_member(message)
    if not admin:
        return
    if not admin["is_admin"]:
        await message.answer("Admin only.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Usage: /allow_leads <telegram_id> or /block_leads <telegram_id>")
        return
    try:
        telegram_id = int(parts[1])
    except ValueError:
        await message.answer("telegram_id должен быть числом")
        return

    target = get_member_by_telegram_id(telegram_id)
    if not target:
        await message.answer("Member not found.")
        return

    command = message.text.split()[0].split("@")[0]
    allowed = command == "/allow_leads"
    (
        supabase.table("team_members")
        .update({"can_receive_leads": allowed})
        .eq("id", target["id"])
        .execute()
    )
    await message.answer(
        f"Lead reception for {target['display_name']}: "
        f"{'enabled' if allowed else 'disabled'}"
    )

