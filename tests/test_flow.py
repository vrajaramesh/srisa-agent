"""agent/flow.py: stage transitions, missing fields, backward moves, versions (pure, no I/O)."""

import uuid
from decimal import Decimal

import pytest

from app.agent import flow
from app.models import Conversation, OrderDraft

GROUPS = {"NECK", "SLEEVE"}
BY_GROUP = {9: "NECK", 10: "NECK", 4: "SLEEVE", 5: "SLEEVE"}


def draft(**kw) -> OrderDraft:
    base = dict(id=uuid.uuid4(), status="open", version=1, option_ids=[], qty=1, design_code="KUR-101")
    return OrderDraft(**{**base, **kw})


def conv(stage: str) -> Conversation:
    return Conversation(id=uuid.uuid4(), external_user_id="919876543210", stage=stage)


SIZED = dict(size_code="M", height_cm=Decimal("160"), fit_preference="REGULAR")
ADDRESSED = dict(delivery_name="Asha", address_line1="1 MG Road", city="Hyderabad", state="Telangana",
                 pincode="500001", delivery_phone="+919876543210")


def test_sizing_needs_size_height_fit() -> None:
    d = draft(size_code="M")
    assert flow.missing_for(flow.SIZING, d) == ["height_cm", "fit_preference"]
    assert flow.next_stage(flow.SIZING, d) == flow.SIZING
    d.height_cm, d.fit_preference = Decimal("160"), "SNUG"
    assert flow.next_stage(flow.SIZING, d) == flow.CUSTOMISE


def test_customise_requires_every_group_and_explicit_finish() -> None:
    d = draft(**SIZED, option_ids=[9])
    assert flow.missing_for(flow.CUSTOMISE, d, GROUPS, BY_GROUP) == ["option:SLEEVE"]
    assert flow.next_stage(flow.CUSTOMISE, d, GROUPS, BY_GROUP, finished_customisation=True) == flow.CUSTOMISE
    d.option_ids = [9, 5]
    assert flow.next_stage(flow.CUSTOMISE, d, GROUPS, BY_GROUP) == flow.CUSTOMISE  # not without finish
    assert flow.next_stage(flow.CUSTOMISE, d, GROUPS, BY_GROUP, finished_customisation=True) == flow.ADDRESS


def test_design_without_option_groups_can_finish() -> None:
    assert flow.next_stage(flow.CUSTOMISE, draft(**SIZED), set(), {}, finished_customisation=True) == flow.ADDRESS


def test_address_needs_all_fields_and_a_quote() -> None:
    d = draft(**SIZED, **ADDRESSED)
    assert flow.next_stage(flow.ADDRESS, d) == flow.ADDRESS  # no quote yet
    d.last_quote = {"breakdown": {}}
    assert flow.next_stage(flow.ADDRESS, d) == flow.CONFIRM
    d.city = None
    assert flow.missing_for(flow.ADDRESS, d) == ["city"]


def test_missing_fields_lists_everything_up_to_confirm() -> None:
    d = draft(size_code="M")
    missing = flow.missing_fields(flow.SIZING, d, GROUPS, BY_GROUP)
    assert missing[:2] == ["height_cm", "fit_preference"]
    assert "option:NECK" in missing and "pincode" in missing and missing[-1] == "quote"
    assert flow.missing_fields(flow.BROWSING, d) == []


def test_losing_sizing_data_sends_later_stages_back_to_sizing() -> None:
    d = draft(**{**SIZED, "fit_preference": None}, **ADDRESSED)
    assert flow.next_stage(flow.ADDRESS, d) == flow.SIZING


def test_touch_increments_version_and_invalidates_summary_and_quote() -> None:
    d = draft(version=3, summary_sent_version=3, last_quote={"x": 1})
    assert flow.summary_awaiting_confirmation(d)
    flow.touch(d)
    assert (d.version, d.summary_sent_version, d.last_quote) == (4, None, None)
    assert not flow.summary_awaiting_confirmation(d)


@pytest.mark.parametrize("current,target,ok", [
    (flow.CONFIRM, flow.SIZING, True), (flow.CONFIRM, flow.CUSTOMISE, True), (flow.CONFIRM, flow.ADDRESS, True),
    (flow.ADDRESS, flow.CUSTOMISE, True), (flow.CUSTOMISE, flow.SIZING, True),
    (flow.SIZING, flow.ADDRESS, False),            # never forward
    (flow.ORDER_CREATED, flow.SIZING, False),      # never after the order exists
    (flow.BROWSING, flow.SIZING, False),
    (flow.CONFIRM, flow.CONFIRM, False),           # not a back target
])
def test_backward_transitions(current: str, target: str, ok: bool) -> None:
    assert flow.can_go_back(current, target) is ok


def test_go_back_moves_stage_and_touches() -> None:
    c, d = conv(flow.CONFIRM), draft(version=5, summary_sent_version=5)
    flow.go_back(c, d, flow.ADDRESS)
    assert c.stage == flow.ADDRESS and d.version == 6 and d.summary_sent_version is None
    with pytest.raises(flow.FlowError):
        flow.go_back(conv(flow.ORDER_CREATED), draft(), flow.SIZING)


def test_unknown_stage_counts_as_browsing() -> None:
    assert flow.stage_of(conv("new")) == flow.BROWSING
    with pytest.raises(flow.FlowError):
        flow.set_stage(conv(flow.BROWSING), "shipping")


def test_draft_summary_only_filled_fields() -> None:
    summary = flow.draft_summary(draft(size_code="M", height_cm=Decimal("160.0"), option_ids=[9]))
    assert summary == {"design_code": "KUR-101", "size_code": "M", "height_cm": 160.0, "qty": 1, "option_ids": [9]}
