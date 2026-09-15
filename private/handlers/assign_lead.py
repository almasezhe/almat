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


def preview_category_queue(category_id: str) -> dict[str, Any]:
    """Use the same read-only SQL planner that actual assignments use."""
    result = supabase.rpc(
        "plan_category_queue", {"p_category_id": category_id}
    ).execute()
    return result.data or {"next_member": None, "compensations": [], "skips": []}
