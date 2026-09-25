"""UTC 与院区时区之间的换算工具。

所有时间在库内以 UTC ISO 字符串保存；班次归属日期、通知窗口等
“本地语义”按站点配置的 IANA 时区计算，使跨午夜班次归入正确日期。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def parse_utc(value: str) -> datetime:
    """解析服务统一使用的 UTC ISO 字符串（接受 Z 后缀）。"""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed.astimezone(timezone.utc)


def format_utc(value: datetime) -> str:
    """格式化为服务统一的 UTC 字符串。"""

    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def local_date(value: datetime, timezone_name: str) -> str:
    """返回某 UTC 时刻在院区时区下的日历日期（YYYY-MM-DD）。"""

    return value.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def local_datetime(value: datetime, timezone_name: str) -> datetime:
    """把 UTC 时刻转到院区本地时间。"""

    return value.astimezone(ZoneInfo(timezone_name))


def shift_local_date(starts_at: datetime, ends_at: datetime, timezone_name: str) -> str:
    """跨午夜班次的归属日期：取开始时刻的院区本地日期。

    例如院区 Asia/Shanghai，22:00 开始、次日 06:00 结束的夜班归入开始当天。
    """

    if ends_at <= starts_at:
        raise ValueError("班次结束时间必须晚于开始时间")
    return local_date(starts_at, timezone_name)


def within_window(moment: datetime, window_start: str | None, window_end: str | None) -> bool:
    """判断时刻是否落在通知窗口内；窗口端点为院区本地 HH:MM。

    跨午夜窗口（start > end）表示从 start 到午夜、再从午夜到 end。
    窗口为空表示全天允许。moment 由调用处换算到院区本地时区。
    """

    if not window_start and not window_end:
        return True
    if not window_start or not window_end:
        raise ValueError("通知窗口必须同时给出开始与结束")
    start = time.fromisoformat(window_start)
    end = time.fromisoformat(window_end)
    local = moment.timetz().replace(tzinfo=None)
    if start <= end:
        return start <= local <= end
    return local >= start or local <= end


def next_window_open(now_utc: datetime, timezone_name: str,
                     window_start: str | None, window_end: str | None) -> datetime:
    """计算下一个窗口开启时刻（UTC）。窗口为空时立即可发。

    无论普通窗口还是跨午夜窗口，规则一致：早于今日窗口起点则今日开启，
    否则次日开启。
    """

    if not window_start:
        return now_utc
    start = time.fromisoformat(window_start)
    local = now_utc.astimezone(ZoneInfo(timezone_name))
    today_start = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    if local < today_start:
        nxt = today_start
    else:
        nxt = today_start + timedelta(days=1)
    return nxt.astimezone(timezone.utc)


def minutes_between(later: datetime, earlier: datetime) -> int:
    return int((later - earlier).total_seconds() // 60)


def add_minutes(value: datetime, minutes: int) -> datetime:
    return value + timedelta(minutes=minutes)
