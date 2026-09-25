"""提供测试与演练使用的可调时钟。"""

from __future__ import annotations

from datetime import datetime, timezone


class SteppingClock:
    """时间只能通过 set 显式推进，便于模拟重试与超时。"""

    def __init__(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("时间必须包含时区")
        self.value = value.astimezone(timezone.utc)

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("时间必须包含时区")
        self.value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self.value
