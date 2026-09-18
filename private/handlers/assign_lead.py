from __future__ import annotations

from typing import Any

from database import supabase


def create_and_assign_lead(
    lead_url: str,
    category_id: str,
    submitter_id: str,
    submitter_username: str | None,
) -> dict[str, Any]:
    """Create a lead and distribute it atomically inside PostgreSQL."""
    result = (
        supabase.rpc(
            "create_lead_atomic",
            {
                "p_lead_url": lead_url.strip(),
                "p_category_id": category_id,
                "p_submitter_id": submitter_id,
                "p_submitter_username": submitter_username,
            },
        )
        .execute()
    )
    return result.data or {}


def change_lead_category(
    lead_url: str,
    old_category_id: str,
    new_category_id: str,
    actor_id: str,
) -> dict[str, Any]:
    """Change a lead category and create compensation/skip atomically."""
    result = (
        supabase.rpc(
            "change_lead_category_atomic",
            {
                "p_lead_url": lead_url.strip(),
                "p_old_category_id": old_category_id,
                "p_new_category_id": new_category_id,
                "p_actor_id": actor_id,
            },
        )
        .execute()
    )
    return result.data or {}


def preview_category_queue(category_id: str, count: int = 3) -> dict[str, Any]:
    """Forecast upcoming assignments without consuming turns or adjustments."""
    if not 1 <= count <= 10:
        raise ValueError("count must be between 1 and 10")
    result = supabase.rpc(
        "preview_category_queue_next", {"p_category_id": category_id, "p_count": count}
    ).execute()
    return result.data or {"next_members": [], "compensations": [], "skips": []}
