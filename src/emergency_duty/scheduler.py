"""后台维护线程：周期性处理到期投递并关闭超期人工任务。

状态全部在 SQLite 中，因此进程重启后只需重新启动该线程，
未完成的通知重试、窗口等待与人工处置就会继续推进。
"""

from __future__ import annotations

import threading
from typing import Callable

from .service import EmergencyDutyService


class MaintenanceWorker:
    """以固定间隔运行 EmergencyDutyService.run_maintenance 的守护线程。"""

    def __init__(self, service: EmergencyDutyService, interval_seconds: float = 30.0,
                 sink: Callable[[dict[str, int]], None] | None = None) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self.sink = sink or (lambda result: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, *, run_immediately: bool = True) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ed-maintenance",
                                        args=(run_immediately,), daemon=True)
        self._thread.start()

    def _loop(self, run_immediately: bool) -> None:
        if run_immediately:
            self._tick()
        while not self._stop.wait(self.interval_seconds):
            self._tick()

    def _tick(self) -> None:
        try:
            self.sink(self.service.run_maintenance())
        except Exception:
            # 单次维护失败不能杀死线程；下个周期继续。
            pass

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
