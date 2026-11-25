"""Normalize source facts without inventing headcounts or publication times."""

import re
from datetime import date, datetime, timedelta, timezone

# Modern Zhaopin dates use Beijing time. A fixed offset also works on Windows
# installations without an IANA timezone database.
BEIJING_TIME = timezone(timedelta(hours=8))
HIRING_TEXT_PATTERN = re.compile(
    r"(?:招聘人数\s*[:：]?\s*|招(?:聘)?\s*)"
    r"(?:\d+(?:\s*[-—–至~～]\s*\d+)?|若干)\s*(?:人|名)"
)
PUBLICATION_TEXT_PATTERN = re.compile(
    r"^(?:(?:发布(?:时间|日期)?\s*[:：]?|发布于)\s*)?"
    r"(?:今天|今日|昨天|昨日|前天|刚刚|\d+\s*(?:天|小时|分钟)前|"
    r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:[ T].+)?)\s*(?:发布)?$"
)


def parse_hiring_count(text: str) -> int | None:
    """Only a single explicit count is numeric; ranges/若干 remain unknown."""
    match = re.fullmatch(
        r"\s*(?:招聘人数\s*[:：]?\s*)?(?:招(?:聘)?\s*)?(\d+)\s*(?:人|名)?\s*",
        text,
    )
    return int(match.group(1)) if match else None


def parse_publish_time(text: str, observed_at: datetime) -> tuple[date | None, datetime | None]:
    """Return a source date and, only for an explicit clock time, a timestamp.

    Relative expressions derive a calendar date from the same observation used
    by collectedAt. Original text is retained separately, including expressions
    whose meaning or precision cannot be resolved.
    """
    value = text.strip()
    if not value or "更新" in value or "刷新" in value:
        return None, None
    value = re.sub(r"^(?:发布时间|发布日期|发布于|发布)\s*[:：]?\s*", "", value)
    value = re.sub(r"\s*发布$", "", value).strip()
    reference = observed_at.astimezone(BEIJING_TIME)
    days = {"今天": 0, "今日": 0, "刚刚": 0, "昨天": 1, "昨日": 1, "前天": 2}
    if value in days:
        return reference.date() - timedelta(days=days[value]), None
    relative = re.fullmatch(r"(\d+)\s*(天|小时|分钟)前", value)
    if relative:
        amount = int(relative.group(1))
        try:
            unit = {"天": "days", "小时": "hours", "分钟": "minutes"}[relative.group(2)]
            delta = timedelta(**{unit: amount})
            return (reference - delta).date(), None
        except OverflowError:
            return None, None
    value = value.replace("年", "-").replace("月", "-").replace("日", "")
    value = value.replace("/", "-").replace(".", "-")
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value):
            return datetime.strptime(value, "%Y-%m-%d").date(), None
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?", value):
            for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    timestamp = datetime.strptime(value.replace("T", " "), pattern)
                    timestamp = timestamp.replace(tzinfo=BEIJING_TIME)
                    return timestamp.date(), timestamp
                except ValueError:
                    continue
    except ValueError:
        pass
    return None, None
