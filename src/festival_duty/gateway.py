"""定义家属联络投递网关的抽象与默认实现。"""

from __future__ import annotations

from typing import Any, Protocol


class DeliveryFailure(Exception):
    """投递渠道本次未能完成送达，调用方据此安排重试或人工处置。"""


class DeliveryGateway(Protocol):
    """定义投递网关的最小接口，便于替换为短信、电话或测试实现。"""

    def deliver(self, *, notification: dict[str, Any], authorization: dict[str, Any],
                summary: str) -> str:
        """执行一次投递并返回渠道回执描述，失败时抛出 DeliveryFailure。"""


class LoopbackGateway:
    """默认网关：不接触外部渠道，直接视为送达，便于离线运行与验收。"""

    def deliver(self, *, notification: dict[str, Any], authorization: dict[str, Any],
                summary: str) -> str:
        return "loopback"
