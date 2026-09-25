"""家属通知投递通道抽象。

生产环境可注入短信/电话/IM 的真实实现；离线验收与测试使用内存记录通道，
失败通道用于验证有限重试与人工处置链。
"""

from __future__ import annotations

from typing import Protocol


class NotificationTransport(Protocol):
    """通知通道需要实现的最小接口。"""

    def send(self, *, channel: str, target: str, subject: str, content: str) -> None:
        """成功返回 None，失败抛出异常。"""


class RecordingTransport:
    """记录全部成功投递的内存通道。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, *, channel: str, target: str, subject: str, content: str) -> None:
        self.sent.append({"channel": channel, "target": target,
                          "subject": subject, "content": content})


class FailingTransport:
    """始终投递失败的通道，用于验证退避重试与转人工。"""

    def __init__(self, message: str = "通道暂时不可用") -> None:
        self.message = message
        self.attempts = 0

    def send(self, *, channel: str, target: str, subject: str, content: str) -> None:
        self.attempts += 1
        raise RuntimeError(self.message)


class FlakyTransport:
    """前 fail_times 次失败、之后成功的通道。"""

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def send(self, *, channel: str, target: str, subject: str, content: str) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("临时故障")
