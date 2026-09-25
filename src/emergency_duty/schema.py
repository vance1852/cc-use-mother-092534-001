"""急诊值守模块的 SQLite 表结构。

所有表通过 site_id 与基础层 sites 关联；状态全部落库，进程重启后
调度器可以继续处理待发送通知、待人工处置投递和待生效换岗。
"""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS ed_patients (
    patient_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    care_state TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 岗位资格：操作者在某院区担任某岗位的有效期（UTC，含两端）。
CREATE TABLE IF NOT EXISTS ed_qualifications (
    qualification_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    position_code TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 班次：版本化；每个院区同一时刻至多一个生效版本（version 最大且 active）。
CREATE TABLE IF NOT EXISTS ed_shifts (
    shift_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    shift_date TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    local_date TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    superseded_by TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, shift_date, version)
);
-- 班次上的岗位责任，assignment_id 是交班事项的转移目标。
CREATE TABLE IF NOT EXISTS ed_shift_assignments (
    assignment_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    position_code TEXT NOT NULL,
    holder_actor_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    UNIQUE(shift_id, sequence_no)
);
-- 患者家属联络授权：撤回只翻转 revoked，历史访问记录仍然保留。
CREATE TABLE IF NOT EXISTS ed_contact_authorizations (
    authorization_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    contact_name TEXT NOT NULL,
    contact_channel TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    window_start TEXT,
    window_end TEXT,
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
    revoked_by TEXT,
    revoked_at TEXT
);
-- 授权范围内的实际历史披露，撤回不删除这些行。
CREATE TABLE IF NOT EXISTS ed_authorization_access (
    access_id TEXT PRIMARY KEY,
    authorization_id TEXT NOT NULL,
    notification_request_id TEXT,
    summary TEXT NOT NULL,
    disclosed_by TEXT NOT NULL,
    disclosed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ed_handover_items (
    item_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    patient_id TEXT,
    position_code TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    critical INTEGER NOT NULL DEFAULT 0 CHECK(critical IN (0, 1)),
    status TEXT NOT NULL,
    assignment_id TEXT,
    transferred_to TEXT,
    transferred_at TEXT,
    completed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 临时换岗：提议 -> 双方确认 -> 生效，每一步原子推进。
CREATE TABLE IF NOT EXISTS ed_handovers (
    handover_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    shift_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    position_code TEXT NOT NULL,
    outgoing_actor_id TEXT NOT NULL,
    incoming_actor_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    outgoing_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(outgoing_confirmed IN (0, 1)),
    incoming_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(incoming_confirmed IN (0, 1)),
    effective_at TEXT,
    blocked_reason TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 通知请求：幂等主记录，重放相同内容安全，不同内容冲突。
CREATE TABLE IF NOT EXISTS ed_notification_requests (
    request_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    authorization_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    subject TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    not_before TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 投递尝试：失败不无限重试，超过 deadline 进入有期限人工处置。
CREATE TABLE IF NOT EXISTS ed_notification_deliveries (
    delivery_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    attempted_at TEXT,
    next_attempt_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0
);
-- 人工处置队列：有期限，过期关闭。
CREATE TABLE IF NOT EXISTS ed_manual_tasks (
    task_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    resolved_by TEXT,
    resolution_note TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
"""
