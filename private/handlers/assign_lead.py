from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from database import supabase

logger = logging.getLogger(__name__)


def create_and_assign_lead(
    lead_url: str,
    category_id: str,
    submitter_id: str,
    submitter_username: str | None,
) -> dict[str, Any]:
    result = supabase.rpc(
        "create_lead_atomic",
        {
            "p_lead_url": lead_url.strip(),
            "p_category_id": category_id,
            "p_submitter_id": submitter_id,
            "p_submitter_username": submitter_username,
        },
    ).execute()

    return result.data or {}


def change_lead_category(
    lead_url: str,
    old_category_id: str,
    new_category_id: str,
    actor_id: str,
) -> dict[str, Any]:
    result = supabase.rpc(
        "change_lead_category_atomic",
        {
            "p_lead_url": lead_url.strip(),
            "p_old_category_id": old_category_id,
            "p_new_category_id": new_category_id,
            "p_actor_id": actor_id,
        },
    ).execute()

    return result.data or {}


def preview_category_queue(
    category_id: str,
    count: int = 3,
) -> dict[str, Any]:
    if type(count) is not int or not 1 <= count <= 10:
        raise ValueError("count must be an integer between 1 and 10")

    for attempt in range(3):
        try:
            result = supabase.rpc(
                "preview_category_queue_next",
                {
                    "p_category_id": category_id,
                    "p_count": count,
                },
            ).execute()

            return result.data or {
                "next_members": [],
                "compensations": [],
                "skips": [],
            }

        except (
            httpx.NetworkError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
        ) as exc:
            if attempt == 2:
                raise

            delay = 0.5 * (2 ** attempt)

            logger.warning(
                "Queue preview transport failure: %s; "
                "retry %s/2 in %.1fs",
                type(exc).__name__,
                attempt + 1,
                delay,
            )

            time.sleep(delay)
