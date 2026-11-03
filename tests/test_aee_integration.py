"""تست‌های یکپارچهٔ فاز ۱ — ضدنشتی لایهٔ ویژگی + تقویم روی دادهٔ واقعی مخزن."""
import sys

sys.path.insert(0, "/home/z/my-project/sweep-dev")

import numpy as np
import pandas as pd
import pytest

from aee.calendar import CalendarFeatures, load_ff_json
from aee.features import build_features
from engine.data.providers import load_mt5_csv

SNAP = pd.Timestamp("2026-11-03T12:00:00Z")


@pytest.fixture(scope="module")
def eur():
    df = load_mt5_csv("data_duka/EURUSD_M15.csv")
    return df[df["time"] >= "2026-10-01"].reset_index(drop=True)


@pytest.fixture(scope="module")
def cal():
    return load_ff_json("data_cal/ff_calendar_2026-11-03.json", fetched_at=SNAP)


def test_features_prefix_invariant_on_real_data(eur):
    full = build_features(eur)
    for k in (100, 500):
        part = build_features(eur.iloc[:k].reset_index(drop=True))
        cols = full.columns
        np.testing.assert_allclose(
            full.iloc[:k - 1][cols].to_numpy(dtype=float),
            part.iloc[:k - 1][cols].to_numpy(dtype=float),
            rtol=1e-10, atol=1e-12, equal_nan=True,
        )


def test_calendar_neutral_for_backtest_window(eur, cal):
    """کندل‌ها قبل از اسنپ‌شات‌اند → ویژگی پیش‌نگر باید کاملاً خنثی باشد."""
    cf = CalendarFeatures(cal, snapshot_time=SNAP)
    feats = cf.build(eur)
    assert feats["minutes_to_next_high"].isna().all()
    assert (feats["pre_release"] == 0).all()


def test_backward_uses_only_actual_events(eur, cal):
    """پس‌نگر فقط از رویدادهای <= اسنپ‌شات؛ برش تقویم به گذشته نتیجه را عوض نکند."""
    cf_full = CalendarFeatures(cal, snapshot_time=SNAP)
    f1 = cf_full.build(eur)
    cf_trunc = CalendarFeatures(cal[cal["time_utc"] < SNAP], snapshot_time=SNAP)
    f2 = cf_trunc.build(eur)
    pd.testing.assert_frame_equal(f1, f2)
    # و مقدار پس‌نگر واقعاً فاصله تا آخرین رویداد تحقق‌یافته است
    last_ev = cal[(cal["time_utc"] < SNAP) &
                  (cal["time_utc"] <= eur["time"].max())]["time_utc"].max()
    idx_last = eur.index[eur["time"] > last_ev][0]
    expected = (eur["time"].iloc[idx_last] - last_ev).total_seconds() / 60
    assert f1["minutes_since_high"].iloc[idx_last] == pytest.approx(expected)


def test_calendar_forward_correct_after_snapshot(cal):
    """بعد از اسنپ‌شات: دقیقه تا پرباثرِ بعدی درست محاسبه شود."""
    cf = CalendarFeatures(cal, snapshot_time=SNAP)
    sched = cal[(cal["time_utc"] >= SNAP) & (cal["impact"] == "High")]["time_utc"]
    t0 = (sched.min() - pd.Timedelta(minutes=20)).to_pydatetime().replace(tzinfo=None)
    df = pd.DataFrame({"time": [pd.Timestamp(t0, tz="UTC")]})
    feats = cf.build(df, impacts=["High"])
    assert feats["minutes_to_next_high"].iloc[0] == pytest.approx(20.0)
    assert feats["pre_release"].iloc[0] == 1.0


def test_lookahead_no_future_calendar_leak(eur, cal):
    """حذف رویدادهای آینده از تقویم نباید ویژگی گذشته را تغییر دهد (خاصیت کوزال)."""
    cf_full = CalendarFeatures(cal, snapshot_time=SNAP)
    cal_past = cal[cal["time_utc"] < SNAP]          # بدون هیچ رویداد آینده
    cf_past = CalendarFeatures(cal_past, snapshot_time=SNAP)
    f1 = cf_full.build(eur.head(300))
    f2 = cf_past.build(eur.head(300))
    pd.testing.assert_frame_equal(f1, f2)
