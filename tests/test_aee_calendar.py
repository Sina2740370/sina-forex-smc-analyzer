"""تست‌های فاز ۱ — تقویم اقتصادی کوزال: اسنپ‌شات، پیش‌نگر/پس‌نگر، ضدنشتی."""
import json
import sys

sys.path.insert(0, "/home/z/my-project/sweep-dev")

import pandas as pd
import pytest

from aee.calendar import CalendarFeatures, load_ff_json

SNAP = pd.Timestamp("2026-11-03T12:00:00Z")

FF_SAMPLE = [
    {"title": "CPI y/y", "country": "USD", "date": "2026-11-04T08:30:00-05:00", "impact": "High"},
    {"title": "Rate Decision", "country": "EUR", "date": "2026-11-02T07:45:00-05:00", "impact": "High"},
    {"title": "Retail Sales", "country": "GBP", "date": "2026-11-03T02:00:00-05:00", "impact": "Medium"},
]


@pytest.fixture
def cal(tmp_path):
    p = tmp_path / "ff.json"
    json.dump(FF_SAMPLE, open(p, "w"))
    return load_ff_json(str(p), fetched_at=SNAP)


def _times(*iso):
    return pd.DatetimeIndex([pd.Timestamp(x) for x in iso])


def test_load_normalizes_to_utc(cal):
    assert list(cal.columns) == ["time_utc", "currency", "impact", "title", "fetched_at"]
    assert all(t.tz is not None for t in cal["time_utc"])
    cpi = cal[cal["title"] == "CPI y/y"].iloc[0]
    assert cpi["time_utc"] == pd.Timestamp("2026-11-04T13:30:00Z")
    assert (cal["fetched_at"] == SNAP).all()
    # مرتب‌سازی زمانی
    assert cal["time_utc"].is_monotonic_increasing


def test_sched_vs_actual_split(cal):
    assert set(cal[cal["time_utc"] >= SNAP]["title"]) == {"CPI y/y"}
    assert set(cal[cal["time_utc"] < SNAP]["title"]) == {"Rate Decision", "Retail Sales"}


def test_forward_feature_neutral_before_snapshot(cal):
    """قاعدهٔ ضدنشتی: قبل از اسنپ‌شات، ویژگی پیش‌نگر کاملاً خنثی است."""
    cf = CalendarFeatures(cal, snapshot_time=SNAP)
    times = _times("2026-11-02T12:00:00Z", "2026-11-03T06:00:00Z")
    nxt = cf.minutes_to_next_release(times)
    assert nxt.isna().all()


def test_forward_feature_after_snapshot(cal):
    cf = CalendarFeatures(cal, snapshot_time=SNAP)
    times = _times("2026-11-04T13:00:00Z", "2026-11-04T13:30:00Z")
    nxt = cf.minutes_to_next_release(times)
    assert nxt.iloc[0] == pytest.approx(30.0)      # تا CPI ساعت 13:30Z
    assert nxt.iloc[1] == pytest.approx(0.0)       # دقیقاً لحظهٔ انتشار
    # افق ۲۴ ساعت: بعد از آن NaN
    far = cf.minutes_to_next_release(_times("2026-11-05T20:00:00Z"))
    assert far.isna().all()


def test_backward_feature_is_causal(cal):
    """پس‌نگر: فقط رویدادهای <= t؛ افزودن رویداد آینده نباید گذشته را عوض کند."""
    cf = CalendarFeatures(cal, snapshot_time=SNAP)
    t1 = _times("2026-11-02T13:00:00Z")
    since_before = cf.minutes_since_last_release(t1)
    # آخرین رویداد قبل: EUR 12:45Z همان روز → 15 دقیقه
    assert since_before.iloc[0] == pytest.approx(15.0)
    # رویدادِ قبل از آخرین انتشار → NaN (پس‌نگرِ واقعی)
    assert cf.minutes_since_last_release(_times("2026-11-02T09:00:00Z")).iloc[0] != \
        cf.minutes_since_last_release(t1).iloc[0]
    # افزودن رویداد جدید بعد از اسنپ‌شات نباید مقدار گذشته را تغییر دهد
    cal2 = pd.concat([cal, pd.DataFrame([{
        "time_utc": pd.Timestamp("2026-11-02T08:00:00Z"), "currency": "JPY",
        "impact": "High", "title": "X", "fetched_at": SNAP}])],
        ignore_index=True)
    cf2 = CalendarFeatures(cal2, snapshot_time=SNAP)
    assert cf2.minutes_since_last_release(t1).iloc[0] == since_before.iloc[0]


def test_build_columns_and_windows(cal):
    cf = CalendarFeatures(cal, snapshot_time=SNAP, pre_minutes=30, post_minutes=60)
    df = pd.DataFrame({"time": list(_times("2026-11-04T13:20:00Z",   # 10 دقیقه قبل CPI
                                            "2026-11-02T12:00:00Z"))})
    feats = cf.build(df, impacts=["High"])
    assert list(feats.columns) == ["minutes_to_next_high", "pre_release",
                                   "post_release", "minutes_since_high"]
    assert feats["pre_release"].iloc[0] == 1.0
    assert feats["post_release"].iloc[0] == 0.0
    # قبل از اسنپ‌شات همه‌چیز خنثی
    assert feats["pre_release"].iloc[1] == 0.0 and feats["minutes_to_next_high"].isna().iloc[1]


def test_currency_and_impact_filter(cal):
    cf = CalendarFeatures(cal, snapshot_time=SNAP)
    # فقط EUR: CPI (USD) نباید دیده شود
    nxt = cf.minutes_to_next_release(_times("2026-11-03T13:00:00Z"), currencies=["EUR"])
    assert nxt.isna().all()
    # فقط Medium: GBP Retail Sales قبل از اسنپ‌شات است → پیش‌نگر خنثی
    nxt2 = cf.minutes_to_next_release(_times("2026-11-03T13:00:00Z"), impacts=["Medium"])
    assert nxt2.isna().all()
