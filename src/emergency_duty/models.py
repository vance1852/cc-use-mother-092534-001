"""急诊值守模块对外暴露的只读数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Patient:
    patient_id: str
    site_id: str
    display_name: str
    care_state: str  # observation | rescue | discharged
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Qualification:
    qualification_id: str
    site_id: str
    actor_id: str
    position_code: str
    valid_from: str
    valid_until: str
    revoked: bool


@dataclass(frozen=True)
class Assignment:
    assignment_id: str
    shift_id: str
    position_code: str
    holder_actor_id: str
    sequence_no: int


@dataclass(frozen=True)
class Shift:
    shift_id: str
    site_id: str
    shift_date: str
    version: int
    starts_at: str
    ends_at: str
    local_date: str
    active: bool
    assignments: tuple[Assignment, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ContactAuthorization:
    authorization_id: str
    patient_id: str
    site_id: str
    contact_name: str
    contact_channel: str
    scope: tuple[str, ...]
    window_start: str | None
    window_end: str | None
    revoked: bool
    granted_by: str
    granted_at: str
    revoked_by: str | None = None
    revoked_at: str | None = None


@dataclass(frozen=True)
class HandoverItem:
    item_id: str
    shift_id: str
    site_id: str
    patient_id: str | None
    position_code: str
    kind: str
    summary: str
    critical: bool
    status: str  # open | transferred | completed
    assignment_id: str | None
    transferred_to: str | None
    transferred_at: str | None
    completed_at: str | None
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Handover:
    handover_id: str
    site_id: str
    shift_id: str
    assignment_id: str
    position_code: str
    outgoing_actor_id: str
    incoming_actor_id: str
    status: str  # proposed | effective | rejected
    reason: str | None
    outgoing_confirmed: bool
    incoming_confirmed: bool
    effective_at: str | None
    blocked_reason: str | None
    created_by: str
    created_at: str


@dataclass(frozen=True)
class NotificationRequest:
    request_id: str
    site_id: str
    patient_id: str
    authorization_id: str
    channel: str
    subject: str
    content: str
    status: str  # pending | delivered | failed | blocked
    not_before: str
    expires_at: str
    created_by: str
    created_at: str
    attempt_count: int = 0
    detail: str | None = None


@dataclass(frozen=True)
class ManualTask:
    task_id: str
    request_id: str
    site_id: str
    reason: str
    status: str  # open | resolved | expired
    deadline_at: str
    resolved_by: str | None
    resolution_note: str | None
    created_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class DutySnapshot:
    """后台接口“当前责任人 + 可披露范围”聚合视图。"""

    site_id: str
    as_of: str
    current_shift: dict[str, Any] | None
    responsible_actors: tuple[dict[str, Any], ...]
    disclosure_scopes: tuple[dict[str, Any], ...]
    pending_contacts: tuple[dict[str, Any], ...]
