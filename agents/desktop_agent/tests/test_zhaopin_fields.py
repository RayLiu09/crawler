from datetime import UTC, date, datetime

import pytest
from desktop_agent.source_adapters.zhaopin.fields import parse_hiring_count, parse_publish_time


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("招3人", 3),
        ("招 2 人", 2),
        ("招聘人数：3人", 3),
        ("招聘人数：招 2 人", 2),
        ("招聘人数：3", 3),
        ("招聘5名", 5),
        ("3人", 3),
        ("招若干人", None),
        ("招3—5人", None),
        ("10000人以上", None),
        ("7107个在招职位", None),
        ("", None),
    ],
)
def test_hiring_count_only_normalizes_explicit_single_count(
    text: str, expected: int | None
) -> None:
    assert parse_hiring_count(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("昨日发布", "2026-11-19"),
        ("昨天发布", "2026-11-19"),
        ("今日发布", "2026-11-20"),
        ("今天", "2026-11-20"),
        ("刚刚发布", "2026-11-20"),
        ("前天发布", "2026-11-18"),
        ("3天前发布", "2026-11-17"),
        ("2小时前发布", "2026-11-19"),
        ("30分钟前发布", "2026-11-19"),
        ("2026-11-19发布", "2026-11-19"),
        ("2026/11/19", "2026-11-19"),
        ("发布日期：2026年11月19日", "2026-11-19"),
        ("54分钟前回复", None),
        ("今日回复50+次", None),
        ("昨日更新", None),
        ("昨天刷新", None),
        ("一个月前发布", None),
        ("2026-02-30发布", None),
        ("99999999999999999999天前发布", None),
        ("", None),
    ],
)
def test_publication_date_uses_beijing_day_without_inventing_clock_time(
    text: str, expected: str | None
) -> None:
    # UTC is still November 19, while Beijing has crossed into November 20.
    observed = datetime(2026, 11, 19, 16, 10, tzinfo=UTC)
    published_date, published_at = parse_publish_time(text, observed)
    assert published_date == (date.fromisoformat(expected) if expected else None)
    assert published_at is None


def test_yesterday_crosses_year_boundary() -> None:
    assert parse_publish_time("昨日发布", datetime(2025, 12, 31, 16, tzinfo=UTC)) == (
        date(2025, 12, 31),
        None,
    )


@pytest.mark.parametrize("text", ["2026-11-19 09:30发布", "发布时间：2026/11/19 09:30:00"])
def test_explicit_publication_clock_time_keeps_source_timezone(text: str) -> None:
    published_date, published_at = parse_publish_time(text, datetime(2026, 11, 20, tzinfo=UTC))
    assert published_date == date(2026, 11, 19)
    assert published_at is not None
    assert published_at.isoformat() == "2026-11-19T09:30:00+08:00"
