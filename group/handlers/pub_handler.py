from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import CommandStart
from aiogram.types import Message

from database import supabase

router = Router()
router.message.filter(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
)


def get_member(telegram_id: int) -> dict | None:
    result = (
        supabase.table("team_members")
        .select("id, is_active")
        .eq("telegram_id", telegram_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


@router.message(CommandStart())
async def start_handler(message: Message):
    # This is the SAME bot as the private bot. This router is only for groups.
    # Any active team member can register a group for team-wide notifications.
    member = get_member(message.from_user.id)
    if not member or not member["is_active"]:
        await message.answer("Сначала зарегистрируйтесь через /start в личке бота.")
        return

    (
        supabase.table("chats")
        .upsert(
            {"chat_id": message.chat.id},
            on_conflict="chat_id",
        )
        .execute()
    )

    await message.answer(
        f"Team group registered.\nChat ID: {message.chat.id}"
    )
