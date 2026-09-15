"""Assign waiting leads and deliver their persistent notices with one bot.

Only deferred assignments use this outbox. Immediate /send assignments keep
their existing notification path, so they are not sent twice by the worker.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from database import supabase

logger = logging.getLogger(__name__)


def _rpc(name: str, arguments: dict | None = None) -> dict:
    return supabase.rpc(name, arguments or {}).execute().data or {}


def _finish_notice(notice: dict, error: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    update = {"lease_until": None, "lease_token": None, "last_error": error}
    if error is None:
        update["sent_at"] = now.isoformat()
    else:
        delay = min(300, 2 ** min(int(notice["attempts"]), 8))
        update["next_attempt_at"] = (now + timedelta(seconds=delay)).isoformat()
    (
        supabase.table("pending_lead_notices")
        .update(update)
        .eq("lead_id", notice["lead_id"])
        .eq("lease_token", notice["lease_token"])
        .execute()
    )


async def process_pending_once(bot) -> None:
    """Bounded batch; DB claims prevent concurrent workers claiming a notice."""
    await asyncio.to_thread(_rpc, "process_pending_leads_atomic", {"p_limit": 20})
    for _ in range(20):
        notice = await asyncio.to_thread(_rpc, "claim_pending_lead_notice")
        if not notice:
            break
        try:
            if not notice.get("telegram_id"):
                raise ValueError("Assigned member has no Telegram ID")
            await asyncio.wait_for(
                bot.send_message(
                    chat_id=int(notice["telegram_id"]),
                    text=(
                        "You have received a queued lead.\n\n"
                        f"Lead #{notice['lead_id']}\n"
                        f"Category: {notice['category_name']}\n"
                        f"Link: {notice['lead_url']}"
                    ),
                    parse_mode=None,
                ),
                timeout=30,
            )
        except asyncio.CancelledError:
            # Do not acknowledge. A new worker recovers the lease after restart.
            raise
        except Exception as exc:
            logger.warning("Queued lead %s delivery failed: %s", notice["lead_id"], exc)
            await asyncio.to_thread(_finish_notice, notice, str(exc)[:1000])
        else:
            await asyncio.to_thread(_finish_notice, notice)


async def pending_leads_worker(bot) -> None:
    while True:
        try:
            await process_pending_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pending-lead worker failed; retrying on next pass")
        await asyncio.sleep(3)