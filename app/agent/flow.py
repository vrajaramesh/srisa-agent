"""Order flow state machine. Pure functions: stages change ONLY through this module (called by tool handlers
and the worker's confirmation path), never because the model says so.

    browsing ─start_order─▶ sizing ─(size+height+fit)─▶ customise ─finish─▶ address ─(details+quote)─▶ confirm
                              ▲                            ▲                    ▲                          │
                              └──────── change_size ───────┴── change_option ───┴──── change_address ──────┤
    order_created ◀──────────────── confirmation (code path in agent/orders.py) ───────────────────────────┘
"""

from datetime import datetime, timezone
from typing import Any

from app.models import Conversation, OrderDraft

BROWSING, SIZING, CUSTOMISE, ADDRESS, CONFIRM, ORDER_CREATED = (
    "browsing", "sizing", "customise", "address", "confirm", "order_created")
STAGES = (BROWSING, SIZING, CUSTOMISE, ADDRESS, CONFIRM, ORDER_CREATED)
ORDER = {s: i for i, s in enumerate(STAGES)}
ORDERING_STAGES = (SIZING, CUSTOMISE, ADDRESS, CONFIRM)
BACK_TARGETS = (SIZING, CUSTOMISE, ADDRESS)

SIZING_FIELDS = ("size_code", "height_cm", "fit_preference")
ADDRESS_FIELDS = ("delivery_name", "address_line1", "city", "state", "pincode", "delivery_phone")

MAX_QTY = 5


class FlowError(Exception):
    """A transition that is not allowed (e.g. going back after the order was created)."""


def stage_of(conversation: Conversation) -> str:
    """Current stage; unknown/legacy values count as browsing."""
    return conversation.stage if conversation.stage in STAGES else BROWSING


def set_stage(conversation: Conversation, stage: str) -> None:
    if stage not in STAGES:
        raise FlowError(f"unknown stage {stage}")
    conversation.stage = stage


def touch(draft: OrderDraft) -> None:
    """Record a change: new version, and any summary/quote sent for the old version is no longer confirmable."""
    draft.version = (draft.version or 0) + 1
    draft.summary_sent_version = None
    draft.last_quote = None
    draft.updated_at = datetime.now(timezone.utc)


def chosen_groups(draft: OrderDraft, option_groups: dict[int, str]) -> set[str]:
    """Option groups the draft has a choice for. option_groups maps option_id -> group for the design."""
    return {option_groups[i] for i in (draft.option_ids or []) if i in option_groups}


def missing_for(stage: str, draft: OrderDraft | None, design_groups: set[str] | None = None,
                option_groups: dict[int, str] | None = None) -> list[str]:
    """What a stage still needs before it can be left."""
    if draft is None:
        return ["design_code"] if stage in ORDERING_STAGES else []
    if stage == SIZING:
        return [f for f in SIZING_FIELDS if getattr(draft, f) in (None, "")]
    if stage == CUSTOMISE:
        missing = sorted((design_groups or set()) - chosen_groups(draft, option_groups or {}))
        return [f"option:{g}" for g in missing] + ([] if 1 <= (draft.qty or 0) <= MAX_QTY else ["qty"])
    if stage == ADDRESS:
        return [f for f in ADDRESS_FIELDS if getattr(draft, f) in (None, "")]
    if stage == CONFIRM:
        return [] if draft.last_quote else ["quote"]
    return []


def missing_fields(stage: str, draft: OrderDraft | None, design_groups: set[str] | None = None,
                   option_groups: dict[int, str] | None = None) -> list[str]:
    """Everything still missing from the current stage up to confirmation (in flow order)."""
    if stage not in ORDERING_STAGES:
        return []
    out: list[str] = []
    for s in ORDERING_STAGES[ORDERING_STAGES.index(stage):]:
        out += missing_for(s, draft, design_groups, option_groups)
    return out


def next_stage(stage: str, draft: OrderDraft | None, design_groups: set[str] | None = None,
               option_groups: dict[int, str] | None = None, finished_customisation: bool = False) -> str:
    """Where the conversation should be now. Moves forward only when the current stage is complete;
    customise is only left via finish_customisation (finished_customisation=True); address -> confirm needs a quote."""
    if draft is None or stage not in ORDERING_STAGES:
        return stage
    if missing_for(SIZING, draft):
        return SIZING if ORDER[stage] >= ORDER[SIZING] else stage
    if stage == SIZING:
        return CUSTOMISE
    if stage == CUSTOMISE:
        complete = not missing_for(CUSTOMISE, draft, design_groups, option_groups)
        return ADDRESS if (finished_customisation and complete) else CUSTOMISE
    if stage == ADDRESS:
        complete = not missing_for(ADDRESS, draft) and draft.last_quote
        return CONFIRM if complete else ADDRESS
    return stage


def can_go_back(current: str, target: str) -> bool:
    """Any ordering stage can return to sizing/customise/address (never forward, never after order_created)."""
    return (target in BACK_TARGETS and current in ORDERING_STAGES and ORDER[target] <= ORDER[current])


def go_back(conversation: Conversation, draft: OrderDraft, target: str) -> None:
    current = stage_of(conversation)
    if not can_go_back(current, target):
        raise FlowError(f"cannot go back from {current} to {target}")
    touch(draft)
    set_stage(conversation, target)


def draft_summary(draft: OrderDraft | None) -> dict[str, Any]:
    """Filled fields only (for facts/progress). Never includes prices except the stored quote total."""
    if draft is None:
        return {}
    fields = ("design_code", "size_code", "height_cm", "fit_preference", "bust_in", "waist_in", "hip_in",
              "measurement_notes", "qty", "delivery_name", "delivery_phone", "address_line1", "address_line2",
              "city", "state", "pincode")
    out = {f: (float(v) if hasattr(v, "is_finite") else v) for f in fields if (v := getattr(draft, f)) not in (None, "")}
    if draft.option_ids:
        out["option_ids"] = list(draft.option_ids)
    return out


def summary_awaiting_confirmation(draft: OrderDraft | None) -> bool:
    return bool(draft and draft.status == "open" and draft.summary_sent_version is not None
                and draft.summary_sent_version == draft.version)
