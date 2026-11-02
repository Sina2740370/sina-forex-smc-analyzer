"""تقسیم‌کنندهٔ Walk-Forward با پاک‌سازی (Purge) و تحریم (Embargo)."""
from __future__ import annotations

import pandas as pd


class PurgedWalkForward:
    """پنجره‌های rolling: [train_start, train_end) → گپ تحریم → [test_start, test_end).

    embargo_bars: تعداد کندلی که بعد از پایان train به‌عنوان گپ کنار گذاشته می‌شود
    تا نشتی اطلاعات از برچسب‌هایی که سرریز زمانی دارند (مثل نگهداری پوزیشن) جلوگیری کند.
    """

    def __init__(self, train_bars: int, test_bars: int, embargo_bars: int = 0,
                 step_bars: int | None = None):
        assert train_bars > 0 and test_bars > 0 and embargo_bars >= 0
        self.train_bars = train_bars
        self.test_bars = test_bars
        self.embargo_bars = embargo_bars
        self.step_bars = step_bars if step_bars is not None else test_bars

    def split(self, index) -> list[dict]:
        n = len(index)
        out = []
        start = 0
        while True:
            train_end = start + self.train_bars
            test_start = train_end + self.embargo_bars
            test_end = test_start + self.test_bars
            if test_start >= n:
                break
            test_end = min(test_end, n)
            out.append({
                "train": index[start:train_end],
                "train_slice": slice(start, train_end),
                "test": index[test_start:test_end],
                "test_slice": slice(test_start, test_end),
            })
            start += self.step_bars
        return out

    def split_by_time(self, index: pd.DatetimeIndex, train_days: int,
                      test_days: int, embargo_hours: float = 0.0) -> list[dict]:
        """نسخهٔ زمانی: طول پنجره‌ها بر حسب روز تقویمی، گپ بر حسب ساعت."""
        index = pd.DatetimeIndex(index)
        out = []
        start = index[0]
        while True:
            train_end = start + pd.Timedelta(days=train_days)
            test_start = train_end + pd.Timedelta(hours=embargo_hours)
            test_end = test_start + pd.Timedelta(days=test_days)
            if test_start >= index[-1]:
                break
            train = index[index < train_end]
            test = index[(index >= test_start) & (index < test_end)]
            if len(train) and len(test):
                out.append({"train": train, "test": test})
            start = start + pd.Timedelta(days=test_days)
        return out
