"""急诊节日值守与家属联络模块的 SQLite 表结构。"""

from __future__ import annotations

from festival_foundation.storage import Database


SCHEMA = """
CREATE TABLE IF NOT EXISTS duty_shifts (
    shift_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    position TEXT NOT NULL,
    holder_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    service_date TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS duty_qualifications (
    qualification_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    position TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT
);
CREATE TABLE IF NOT EXISTS duty_swap_requests (
    swap_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES duty_shifts(shift_id),
    from_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    to_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    from_confirmed_at TEXT,
    to_confirmed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS contact_authorizations (
    authorization_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    patient_ref TEXT NOT NULL,
    contact_name TEXT NOT NULL,
    contact_channel TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    revoked_by TEXT,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS notification_requests (
    notification_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    patient_ref TEXT NOT NULL,
    authorization_id TEXT NOT NULL REFERENCES contact_authorizations(authorization_id),
    scope TEXT NOT NULL,
    summary TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    manual_due_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_attempts (
    attempt_id TEXT PRIMARY KEY,
    notification_id TEXT NOT NULL REFERENCES notification_requests(notification_id),
    attempted_by TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS disclosure_events (
    disclosure_id TEXT PRIMARY KEY,
    authorization_id TEXT NOT NULL REFERENCES contact_authorizations(authorization_id),
    notification_id TEXT,
    site_id TEXT NOT NULL,
    patient_ref TEXT NOT NULL,
    contact_name TEXT NOT NULL,
    scope TEXT NOT NULL,
    channel TEXT NOT NULL,
    disclosed_by TEXT NOT NULL,
    disclosed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover_items (
    item_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    shift_id TEXT NOT NULL REFERENCES duty_shifts(shift_id),
    patient_ref TEXT NOT NULL,
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover_decisions (
    decision_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES handover_items(item_id),
    from_shift_id TEXT NOT NULL,
    to_shift_id TEXT,
    decision TEXT NOT NULL,
    reason TEXT,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
"""


def ensure_schema(database: Database) -> None:
    """在基础层数据库上幂等地建立本模块的表。"""

    database.connection.executescript(SCHEMA)
