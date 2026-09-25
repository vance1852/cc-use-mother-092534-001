"""定义急诊节日值守模块在接口边界使用的数据对象与词表。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# 急诊节日值守覆盖的三类岗位，交班事项沿用同一词表。
POSITIONS = frozenset({"rescue", "observation", "cross_support"})

# 可披露范围按严格程度递增排序，数值越大允许披露的内容越多。
SCOPES = {"identity_only": 1, "condition_summary": 2, "critical_updates": 3, "full": 4}

ITEM_STATUSES = frozenset({"open", "in_rescue", "closed"})

NOTIFICATION_STATUSES = frozenset({"pending", "delivered", "manual_handling", "expired", "abandoned"})

SWAP_STATUSES = frozenset({"pending", "applied", "declined"})

DECISIONS = frozenset({"transferred", "retained_rescue", "manual_transfer_rescue"})


@dataclass(frozen=True)
class Shift:
    """表示一个按院区时区归属服务日的值守班次。"""

    shift_id: str
    site_id: str
    position: str
    holder_actor_id: str
    starts_at: str
    ends_at: str
    service_date: str
    timezone_name: str
    version: int
    created_at: str


@dataclass(frozen=True)
class Authorization:
    """表示一位家属对患者病情信息的联络授权。"""

    authorization_id: str
    site_id: str
    patient_ref: str
    contact_name: str
    contact_channel: str
    scope: str
    status: str
    granted_by: str
    granted_at: str
    revoked_by: Optional[str]
    revoked_at: Optional[str]


@dataclass(frozen=True)
class Notification:
    """表示一次面向家属的病情通知请求及其投递状态。"""

    notification_id: str
    site_id: str
    patient_ref: str
    authorization_id: str
    scope: str
    summary: str
    window_start: str
    window_end: str
    status: str
    attempts: int
    max_attempts: int
    manual_due_at: Optional[str]
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DisclosureEvent:
    """表示一次已经发生的病情披露，授权撤回后仍然保留。"""

    disclosure_id: str
    authorization_id: str
    notification_id: Optional[str]
    site_id: str
    patient_ref: str
    contact_name: str
    scope: str
    channel: str
    disclosed_by: str
    disclosed_at: str


@dataclass(frozen=True)
class HandoverItem:
    """表示一条随班次流转的交班事项。"""

    item_id: str
    site_id: str
    shift_id: str
    patient_ref: str
    category: str
    summary: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class HandoverDecision:
    """表示一次交班中对单个事项作出的决定。"""

    decision_id: str
    batch_id: str
    site_id: str
    item_id: str
    from_shift_id: str
    to_shift_id: Optional[str]
    decision: str
    reason: Optional[str]
    decided_by: str
    decided_at: str
