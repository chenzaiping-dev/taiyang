from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing() -> datetime:
    """北京时间（aware）"""
    return datetime.now(BEIJING_TZ)


def now_beijing_naive() -> datetime:
    """北京时间（naive），适合写入 MySQL DATETIME"""
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def to_beijing_str(dt, fmt="%Y-%m-%d %H:%M:%S") -> str:
    if dt is None:
        return ""
    return dt.strftime(fmt)