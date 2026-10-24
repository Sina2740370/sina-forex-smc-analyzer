"""تست server_tz=ny_close برای LiteFinance (NY wall + 7h با DST)."""
from pathlib import Path

import pandas as pd

from engine.data.providers import load_mt5_csv


def _write_csv(tmp_path: Path, when: str) -> Path:
    """یک ردیف CSV سبک با ستون‌های date+time متاتریدر."""
    d, t = when.split(" ")
    path = tmp_path / "sample.csv"
    path.write_text(
        f"DATE,TIME,OPEN,HIGH,LOW,CLOSE,TICKVOL\n"
        f"{d},{t},1.0,1.1,0.9,1.05,10\n",
        encoding="utf-8",
    )
    return path


def test_ny_close_winter_est(tmp_path):
    """2025-12-01 00:00 سرور → 2025-11-30 22:00 UTC (EST = UTC-5)."""
    path = _write_csv(tmp_path, "2025.12.01 00:00:00")
    df = load_mt5_csv(str(path), server_tz="ny_close")
    assert len(df) == 1
    assert df["time"].iloc[0] == pd.Timestamp("2025-11-30 22:00:00", tz="UTC")


def test_ny_close_summer_edt(tmp_path):
    """2026-07-06 00:00 سرور → 2026-07-05 21:00 UTC (EDT = UTC-4)."""
    path = _write_csv(tmp_path, "2026.07.06 00:00:00")
    df = load_mt5_csv(str(path), server_tz="ny_close")
    assert len(df) == 1
    assert df["time"].iloc[0] == pd.Timestamp("2026-07-05 21:00:00", tz="UTC")


def test_without_server_tz_keeps_legacy_offset(tmp_path):
    """بدون server_tz رفتار قبلی (فقط tz_offset_hours) عیناً."""
    path = _write_csv(tmp_path, "2025.12.01 00:00:00")
    df0 = load_mt5_csv(str(path), tz_offset_hours=0.0)
    df3 = load_mt5_csv(str(path), tz_offset_hours=3.0)
    assert df0["time"].iloc[0] == pd.Timestamp("2025-12-01 00:00:00", tz="UTC")
    assert df3["time"].iloc[0] == pd.Timestamp("2025-11-30 21:00:00", tz="UTC")
