from secrets import token_hex

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from database import supabase


router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)


def is_admin(telegram_id):
    result = (
        supabase.table("team_members")
        .select("id")
        .eq("telegram_id", telegram_id)
        .eq("is_active", True)
        .eq("is_admin", True)
        .limit(1)
        .execute()
    )
    return bool(result.data)


def manage(telegram_id, action, code, name=None, order=None):
    if action not in {"add", "edit", "delete"}:
        return {"status": "invalid_action"}

    return supabase.rpc(
        "manage_category_atomic",
        {
            "p_actor_telegram_id": telegram_id,
            "p_action": action,
            "p_code": code,
            "p_name": name,
            "p_sort_order": order,
        },
    ).execute().data or {}


def result_text(result):
    if result.get("status") == "ok":
        action = {
            "add": "добавлена",
            "edit": "изменена",
            "delete": "отключена",
        }[result["action"]]

        return (
            f"Категория {action}: {result['name']}\n"
            f"Код: {result['code']}\n"
            f"Порядок кнопки: {result['sort_order']}"
        )

    return {
        "forbidden": "Только для администратора.",
        "exists": "Этот код уже занят, в том числе отключённой категорией.",
        "not_found": "Категория не найдена.",
        "already_deleted": "Категория уже отключена.",
        "invalid_code": (
            "Код: 1–50 символов a–z, 0–9, дефис или подчёркивание. "
            "Первый символ — буква или цифра."
        ),
        "invalid_name": "Название должно содержать от 1 до 80 символов.",
        "invalid_order": "Порядок должен быть целым числом от 0 до 1000000.",
    }.get(result.get("status"), "Не удалось выполнить операцию.")


@router.message(Command("categories"))
async def categories_handler(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только для администратора.")
        return

    rows = (
        supabase.table("categories")
        .select("code, name, sort_order, is_active")
        .order("sort_order")
        .order("code")
        .execute()
    ).data or []

    text = (
        "Управление категориями\n\n"
        "/add_category code | Название | порядок\n"
        "/edit_category code | Новое название | порядок\n"
        "/delete_category code\n\n"
        "Порядок можно не указывать.\n"
        "При добавлении категория окажется в конце списка.\n"
        "При изменении прежний порядок сохранится.\n\n"
        "Пример: /add_category nuet | NUET | 20\n\n"
    )

    for row in rows:
        mark = "✓" if row["is_active"] else "✕"
        line = (
            f"{mark} {row['code']} — {row['name']} "
            f"(порядок {row['sort_order']})\n"
        )

        if len(text) + len(line) > 3900:
            await message.answer(text, parse_mode=None)
            text = ""

        text += line

    await message.answer(text, parse_mode=None)


@router.message(
    Command("add_category", "edit_category", "delete_category")
)
async def category_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только для администратора.")
        return

    words = message.text.split(maxsplit=1)
    command = words[0].split("@")[0]
    action = {
        "/add_category": "add",
        "/edit_category": "edit",
        "/delete_category": "delete",
    }[command]

    if len(words) < 2:
        await message.answer("Формат команд: /categories")
        return

    parts = [part.strip() for part in words[1].split("|")]
    code = parts[0]
    name = None
    order = None

    if action in {"add", "edit"}:
        if len(parts) not in {2, 3} or not parts[1]:
            await message.answer(
                f"Формат: /{action}_category code | Название | порядок"
            )
            return

        name = parts[1]

        if len(parts) == 3:
            try:
                order = int(parts[2])
                if not 0 <= order <= 1000000:
                    raise ValueError
            except ValueError:
                await message.answer(
                    "Порядок: целое число от 0 до 1000000."
                )
                return

    elif len(parts) != 1 or not code:
        await message.answer(
            "Укажите только код категории. Список: /categories"
        )
        return

    if action == "delete":
        rows = (
            supabase.table("categories")
            .select("name, is_active")
            .eq("code", code)
            .limit(1)
            .execute()
        ).data or []

        if not rows or not rows[0]["is_active"]:
            await message.answer(
                "Категория не найдена или уже отключена."
            )
            return

        await state.clear()
        token = token_hex(4)

        sent = await message.answer(
            f"Отключить категорию «{rows[0]['name']}»?\n\n"
            "Она исчезнет из выбора категорий.\n"
            "Существующие лиды, очередь и история сохранятся.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Да, отключить",
                            callback_data=f"cat_delete:{token}:yes",
                        ),
                        InlineKeyboardButton(
                            text="Нет",
                            callback_data=f"cat_delete:{token}:no",
                        ),
                    ]
                ]
            ),
            parse_mode=None,
        )

        await state.update_data(
            category_delete={
                "token": token,
                "code": code,
                "message_id": sent.message_id,
            }
        )
        return

    result = manage(message.from_user.id, action, code, name, order)
    await message.answer(result_text(result), parse_mode=None)


@router.callback_query(F.data.startswith("cat_delete:"))
async def delete_confirmation(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "Только для администратора.",
            show_alert=True,
        )
        return

    parts = callback.data.split(":")
    pending = (await state.get_data()).get("category_delete") or {}

    if (
        len(parts) != 3
        or parts[1] != pending.get("token")
        or parts[2] not in {"yes", "no"}
        or callback.message is None
        or callback.message.message_id != pending.get("message_id")
    ):
        await callback.answer(
            "Подтверждение устарело. Повторите команду.",
            show_alert=True,
        )
        return

    if parts[2] == "no":
        text = "Отключение отменено."
    else:
        result = manage(
            callback.from_user.id,
            "delete",
            pending["code"],
        )
        text = result_text(result)

    await state.update_data(category_delete=None)
    await callback.answer()
    await callback.message.edit_text(
        text,
        reply_markup=None,
        parse_mode=None,
    )