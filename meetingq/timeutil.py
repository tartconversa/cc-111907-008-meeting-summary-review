"""统一时钟。

契约要求：所有时间带时区并以 UTC 记录；received 是事实进入系统的时点。
为使测试可重复，时钟可以注入固定时间（Clock.fixed），默认使用真实 UTC。
"""

from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    """可替换的时钟；全部输出以 ``...Z`` 形式的 UTC 字符串给出。"""

    def __init__(self, fixed: str | None = None) -> None:
        self._fixed = fixed

    @classmethod
    def fixed(cls, iso_z: str) -> "Clock":
        # 立即校验格式，避免错误时间被静默写入。
        parse_utc(iso_z)
        return cls(iso_z)

    def now(self) -> str:
        if self._fixed is not None:
            return self._fixed
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str) -> datetime:
    """解析契约中的 UTC 时间字符串（结尾 Z），返回带时区的 datetime。"""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"时间必须是结尾为 Z 的 UTC 字符串: {value!r}")
    dt = datetime.fromisoformat(value[:-1] + "+00:00")
    if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
        raise ValueError(f"时间必须为 UTC: {value!r}")
    return dt


def to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
