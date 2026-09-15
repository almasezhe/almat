from __future__ import annotations

from typing import Any

from database import supabase


def log_event(
    event_type: str,
    *,
    actor_id: str | None = None,
    member_id: str | None = None,
    lead_id: int | None = None,
    category_id: str | None = None,
    old_category_id: str | None = None,
    new_category_id: str | None = None,
    next_member_id: str | None = None,
    old_status: str | None = None,
    new_status: str | None = None,
    actor_name_snapshot: str | None = None,
    member_name_snapshot: str | None = None,
    category_name_snapshot: str | None = None,
    old_category_name_snapshot: str | None = None,
    new_category_name_snapshot: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    payload = {
        "event_type": event_type,
        "actor_id": actor_id,
        "member_id": member_id,
        "lead_id": lead_id,
        "category_id": category_id,
        "old_category_id": old_category_id,
        "new_category_id": new_category_id,
        "next_member_id": next_member_id,
        "old_status": old_status,
        "new_status": new_status,
        "actor_name_snapshot": actor_name_snapshot,
        "member_name_snapshot": member_name_snapshot,
        "category_name_snapshot": category_name_snapshot,
        "old_category_name_snapshot": old_category_name_snapshot,
        "new_category_name_snapshot": new_category_name_snapshot,
        "metadata": metadata or {},
    }

    # Keep the insert explicit but do not send null snapshot fields unnecessarily.
    result = supabase.table("events").insert(payload).execute()
    return result.data[0] if result.data else None
