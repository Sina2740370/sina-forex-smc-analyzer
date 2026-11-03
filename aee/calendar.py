"""تقویم اقتصادی با زمان انتشار — ویژگی‌های خبری اکیداً کوزال.

قاعدهٔ ضدنشتی (مهم):
- «زمان انتشارِ آینده» فقط وقتی می‌تواند در گذشتهٔ تحلیل استفاده شود که در اسنپ‌شاتِ
  تقویمِ گرفته‌شده در لحظهٔ F، رویداد بعد از F باشد (یعنی زمان‌بندی اعلام‌شده است).
  → minutes_to_next_release فقط از رویدادهای time >= snapshot_time استفاده می‌کند.
- رویدادهای قبل از F زمانِ «تحقق واقعی» دارند و صرفِ زمان‌بندی‌بودنشان قابل اثبات نیست؛
  بنابراین فقط در ویژگی پس‌نگر minutes_since_last_release (رویداد <= t) مجازند.
- در بک‌تستِ قبل از تاریخ اسنپ‌شات، ویژگی پیش‌نگر صبحت‌با NaN/خنثی است — این
  عمدی است: تقویم تاریخیِ زمان‌بندی‌شده نیازمند منبع لایسنس‌دار است (PHASE1_RESULTS.md).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

HIGH = "High"
MEDIUM = "Medium"


def load_ff_json(path: str, fetched_at: pd.Timestamp) -> pd.DataFrame:
    """خواندن اسنپ‌شات ForexFactory (ff_calendar_thisweek.json) → جدول استاندارد.

    ستون‌ها: time_utc (tz-aware UTC)، currency، impact، title، fetched_at.
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    rows = []
    fetched_at = pd.Timestamp(fetched_at).tz_localize("UTC") if pd.Timestamp(fetched_at).tz is None else pd.Timestamp(fetched_at)
    for ev in raw:
        t = pd.Timestamp(ev["date"])
        if t.tz is None:
            t = t.tz_localize("UTC")
        t = t.tz_convert("UTC")
        rows.append({
            "time_utc": t,
            "currency": str(ev.get("country", "")).upper(),
            "impact": str(ev.get("impact", "")),
            "title": str(ev.get("title", "")),
            "fetched_at": fetched_at,
        })
    df = pd.DataFrame(rows).sort_values("time_utc").reset_index(drop=True)
    return df


def load_csv(path: str, fetched_at: pd.Timestamp) -> pd.DataFrame:
    """خواندن تقویم از CSV با همان اسکیمای استاندارد (برای منابع دیگر)."""
    df = pd.read_csv(path)
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    df["fetched_at"] = pd.Timestamp(fetched_at)
    return df.sort_values("time_utc").reset_index(drop=True)


class CalendarFeatures:
    """ویژگی‌های خبری روی فریم M15؛ خنثی = بدون نشتی."""

    def __init__(self, calendar: pd.DataFrame, snapshot_time: pd.Timestamp,
                 pre_minutes: int = 30, post_minutes: int = 60):
        self.cal = calendar.sort_values("time_utc").reset_index(drop=True)
        self.snapshot = pd.Timestamp(snapshot_time)
        if self.snapshot.tz is None:
            self.snapshot = self.snapshot.tz_localize("UTC")
        self.pre = pre_minutes
        self.post = post_minutes

    def _sched(self) -> pd.DataFrame:
        """رویدادهای زمان‌بندی‌شده: بعد از لحظهٔ اسنپ‌شات."""
        return self.cal[self.cal["time_utc"] >= self.snapshot]

    def _actual(self) -> pd.DataFrame:
        """رویدادهای تحقق‌یافته: قبل یا همزمان لحظهٔ اسنپ‌شات."""
        return self.cal[self.cal["time_utc"] < self.snapshot]

    def _filter(self, df: pd.DataFrame, currencies=None, impacts=None) -> pd.DataFrame:
        out = df
        if currencies:
            out = out[out["currency"].isin({c.upper() for c in currencies})]
        if impacts:
            out = out[out["impact"].isin(impacts)]
        return out

    def minutes_to_next_release(self, times, currencies=None, impacts=None) -> pd.Series:
        """دقیقه تا انتشار بعدیِ زمان‌بندی‌شده؛ NaN اگر در افق ۲۴ ساعته خبری نباشد."""
        sched = self._filter(self._sched(), currencies, impacts)
        out = pd.Series(np.nan, index=pd.DatetimeIndex(times), dtype=float)
        if sched.empty:
            return out
        ev = sched["time_utc"].astype("int64").to_numpy()          # ns از اپوک
        for i, t in enumerate(out.index):
            t = pd.Timestamp(t)
            t = t.tz_convert("UTC") if t.tz is not None else t.tz_localize("UTC")
            if t.value < self.snapshot.value:
                # قبل از اسنپ‌شات، زمان‌بندی‌بودن رویدادهای آینده قابل اثبات نیست → خنثی
                continue
            j = np.searchsorted(ev, t.value, side="left")
            if j < len(ev):
                dt = (ev[j] - t.value) / 6e10
                if dt <= 24 * 60:
                    out.iloc[i] = dt
        return out

    def minutes_since_last_release(self, times, currencies=None, impacts=None) -> pd.Series:
        """دقیقه از آخرین انتشار تحقق‌یافته (فقط رویداد <= t — پس‌نگر)."""
        actual = self._filter(self.cal[self.cal["time_utc"] <= self.snapshot],
                              currencies, impacts)
        out = pd.Series(np.nan, index=pd.DatetimeIndex(times), dtype=float)
        if actual.empty:
            return out
        ev = actual["time_utc"].astype("int64").to_numpy()          # ns از اپوک
        for i, t in enumerate(out.index):
            t = pd.Timestamp(t)
            t = t.tz_convert("UTC") if t.tz is not None else t.tz_localize("UTC")
            j = np.searchsorted(ev, t.value, side="right") - 1
            if j >= 0:
                out.iloc[i] = (t.value - ev[j]) / 6e10
        return out

    def build(self, df: pd.DataFrame, currencies=None, impacts=None) -> pd.DataFrame:
        """خروجی هم‌راستا با df؛ ستون‌ها: minutes_to_next_high، pre_release، post_release."""
        times = pd.DatetimeIndex(df["time"])
        out = pd.DataFrame(index=df.index)
        nxt = self.minutes_to_next_release(times, currencies, impacts).to_numpy()
        prv = self.minutes_since_last_release(times, currencies, impacts).to_numpy()
        out["minutes_to_next_high"] = nxt
        out["pre_release"] = ((nxt <= self.pre) & (nxt >= 0)).astype(float)
        out["post_release"] = ((prv >= 0) & (prv <= self.post)).astype(float)
        out["minutes_since_high"] = prv
        return out
