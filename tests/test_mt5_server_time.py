"""تست server_tz=Europe/Athens برای LiteFinance (DST اروپا)."""
from pathlib import Path

import pandas as pd

from engine.data.providers import load_mt5_csv

TZ = "Europe/Athens"


def _write_csv(tmp_path: Path, when: str) -> Path:
    d, t = when.split(" ")
    path = tmp_path / "sample.csv"
    path.write_text(
        f"DATE,TIME,OPEN,HIGH,LOW,CLOSE,TICKVOL\n"
        f"{d},{t},1.0,1.1,0.9,1.05,10\n",
        encoding="utf-8",
    )
    return path


def test_athens_winter_eet(tmp_path):
    """2025-12-01 00:00 سرور → 2025-11-30 22:00 UTC (EET = UTC+2)."""
    path = _write_csv(tmp_path, "2025.12.01 00:00:00")
    df = load_mt5_csv(str(path), server_tz=TZ)
    assert len(df) == 1
    assert df["time"].iloc[0] == pd.Timestamp("2025-11-30 22:00:00", tz="UTC")


def test_athens_summer_eest(tmp_path):
    """2026-07-06 00:00 سرور → 2026-07-05 21:00 UTC (EEST = UTC+3)."""
    path = _write_csv(tmp_path, "2026.07.06 00:00:00")
    df = load_mt5_csv(str(path), server_tz=TZ)
    assert len(df) == 1
    assert df["time"].iloc[0] == pd.Timestamp("2026-07-05 21:00:00", tz="UTC")


def test_athens_us_dst_gap_week(tmp_path):
    """2026-03-08 23:00 (یکشنبه، هفته‌ی اختلاف DST آمریکا/اروپا) → 2026-03-08 21:00 UTC.

    US DST شروع شده؛ اروپا هنوز EET (UTC+2) است.
    """
    path = _write_csv(tmp_path, "2026.03.08 23:00:00")
    df = load_mt5_csv(str(path), server_tz=TZ)
    assert len(df) == 1
    assert df["time"].iloc[0] == pd.Timestamp("2026-03-08 21:00:00", tz="UTC")


def test_without_server_tz_keeps_legacy_offset(tmp_path):
    """بدون server_tz رفتار قبلی (فقط tz_offset_hours) عیناً."""
    path = _write_csv(tmp_path, "2025.12.01 00:00:00")
    df0 = load_mt5_csv(str(path), tz_offset_hours=0.0)
    df3 = load_mt5_csv(str(path), tz_offset_hours=3.0)
    assert df0["time"].iloc[0] == pd.Timestamp("2025-12-01 00:00:00", tz="UTC")
    assert df3["time"].iloc[0] == pd.Timestamp("2025-11-30 21:00:00", tz="UTC")
