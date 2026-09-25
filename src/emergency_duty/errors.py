"""急诊值守模块的业务异常，补充基础层未覆盖的状态冲突。"""

from __future__ import annotations

from festival_foundation.errors import ConflictError


class HandoverBlockedError(ConflictError):
    """临时换岗因接任资格、双方确认或抢救中事项而不能生效。"""

    code = "handover_blocked"


class DeliveryConflictError(ConflictError):
    """同一通知请求编号被用于不同的通知内容。"""

    code = "delivery_conflict"
