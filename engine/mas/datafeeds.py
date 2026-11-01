"""فاز ۳ — لایه داده بیرونی MAS: FRED (ییلد) + CFTC COT (سنتیمنت).

اصول:
- همه سری‌ها با تاریخ دریافت در کش دیسک ذخیره می‌شوند (data_cot/؛ گیت‌شده).
- هیچ عددی «الانِ» زنده در تحلیل گذشته استفاده نمی‌شود: متدها فقط ردیف‌های ≤ now برمی‌گردانند
  (بدون lookahead) — پیش‌نیاز بک‌تست.
- شبکه در دسترس نبود؟ متدها None می‌دهند و ایجنت مربوطه stub صادقانه می‌شود؛ حدس ممنوع.

سری‌های FRED (بدون کلید، از fredgraph.csv):
- DGS10 (روزانه) / T10YIE (روزانه، تورم انتظاری ۱۰ساله) → ییلد واقعی آمریکا (برای طلا)
- IRLTLT01DEM156N / IRLTLT01GBM156N / IRLTLT01JPM156N (ماهانه، ییلد بلندمدت دولتی) → EA/UK/JP

COT:
- مالی (فارکس): FinFutYY.txt — EUR 099741 / GBP 096742 / JPY 097741؛
  خالص = (Asset Mgr + Leveraged Funds) لانگ − شورت (پوزیشن پول هوشمند)
- کالایی (طلا): annual.txt — GOLD-COMEX 088691؛ خالص = Noncommercial لانگ − شورت
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import urllib.request
import zipfile
from datetime import datetime, timezone

import pandas as pd

FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="
COT_FIN_URL = "https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip"
COT_COM_URL = "https://www.cftc.gov/files/dea/history/deacot{year}.zip"

FRED_SERIES = ["DGS10", "T10YIE", "IRLTLT01DEM156N", "IRLTLT01GBM156N", "IRLTLT01JPM156N"]
COT_FIN_CODES = {"EURUSD": "099741", "GBPUSD": "096742", "USDJPY": "097741"}
COT_COM_CODES = {"XAUUSD": "088691"}

YIELD_SERIES = {   # (سری کوت، سری بِیس) — امتیاز مثبت = بِیس قوی‌تر از کوت
    "EURUSD": ("DGS10", "IRLTLT01DEM156N"),      # diff = EUR − US
    "GBPUSD": ("DGS10", "IRLTLT01GBM156N"),      # diff = GBP − US
    "USDJPY": ("IRLTLT01JPM156N", "DGS10"),      # diff = US − JP
    "XAUUSD": ("T10YIE", "DGS10"),               # diff = ییلد واقعی US؛ fundamental با امضای منفی معکوس می‌کند
}

FIN_POS_COLS = ("Asset_Mgr_Positions_Long_All", "Asset_Mgr_Positions_Short_All",
                "Lev_Money_Positions_Long_All", "Lev_Money_Positions_Short_All")
COM_POS_COLS = ("Noncommercial Positions-Long (All)", "Noncommercial Positions-Short (All)")


def _http(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "mas-engine/1.0 (research)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class MASFeeds:
    """کش دیسکی داده بیرونی. online=False → فقط کش (بدون شبکه)؛ داده نبود → None."""

    def __init__(self, cache_dir: str = "data_cot", online: bool = True,
                 fred_ttl_hours: float = 24.0):
        self.dir = cache_dir
        self.online = online
        self.fred_ttl = fred_ttl_hours * 3600
        os.makedirs(cache_dir, exist_ok=True)
        self._notes: list = []
        self._fin_df: pd.DataFrame | None = None
        self._com_df: pd.DataFrame | None = None
        self._yields: dict = {}

    # ------------------------------------------------------------- FRED
    def _load_fred(self, series_id: str) -> pd.DataFrame | None:
        path = os.path.join(self.dir, f"fred_{series_id}.csv")
        meta = os.path.join(self.dir, f"fred_{series_id}.json")
        fresh = os.path.exists(meta) and time.time() - json.load(open(meta))["ts"] < self.fred_ttl
        if not os.path.exists(path) or (self.online and not fresh):
            try:
                raw = _http(FRED_BASE + series_id)
                open(path, "wb").write(raw)
                json.dump({"ts": time.time()}, open(meta, "w"))
            except Exception as e:
                self._notes.append(f"FRED {series_id}: {type(e).__name__}")
                if not os.path.exists(path):
                    return None
        df = pd.read_csv(path)
        df.columns = ["date", "value"]
        df["date"] = pd.to_datetime(df["date"])
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        return df.dropna().sort_values("date").reset_index(drop=True)

    def _ensure_yields(self) -> bool:
        if self._yields:
            return True
        for sid in set(sum(YIELD_SERIES.values(), ())):
            df = self._load_fred(sid)
            if df is None or df.empty:
                return False
            self._yields[sid] = df
        return True

    def yield_diff(self, symbol: str, now: pd.Timestamp) -> dict | None:
        """تفاضل ییلد (بِیس − کوت؛ طلا: −ییلد واقعی) در آخرین داده ≤ now + تازگی روز."""
        now = pd.Timestamp(now).tz_localize(None) if pd.Timestamp(now).tzinfo else pd.Timestamp(now)
        if not self._ensure_yields():
            return None
        q, b = YIELD_SERIES[symbol]
        qb, bb = self._yields[q], self._yields[b]
        vq = qb[qb["date"] <= now].iloc[-1] if (qb["date"] <= now).any() else None
        vb = bb[bb["date"] <= now].iloc[-1] if (bb["date"] <= now).any() else None
        if vq is None or vb is None:
            return None
        diff = float(vb["value"]) - float(vq["value"])
        sign = -1.0 if symbol == "XAUUSD" else 1.0   # ییلد واقعی بالاتر → طلا ضعیف‌تر
        staleness = float((now - max(vq["date"], vb["date"])).days)
        return {"diff_pct": sign * diff, "asof": str(max(vq["date"], vb["date"]).date()),
                "staleness_days": staleness,
                "series": f"{b} - {q}" + (" (inverted)" if sign < 0 else "")}

    # ------------------------------------------------------------- COT
    def _ensure_cot(self) -> bool:
        if self._fin_df is not None or self._com_df is not None:
            return True
        years = [2023, 2024, 2025, 2026]
        fin_parts, com_parts = [], []
        for y in years:
            p_fin = os.path.join(self.dir, f"fin_{y}.csv")
            if not os.path.exists(p_fin) and self.online:
                try:
                    z = zipfile.ZipFile(io.BytesIO(_http(COT_FIN_URL.format(year=y))))
                    name = z.namelist()[0]
                    pd.read_csv(io.StringIO(z.read(name).decode("latin-1"))).to_csv(p_fin, index=False)
                except Exception as e:
                    self._notes.append(f"COT-fin {y}: {type(e).__name__}")
            if os.path.exists(p_fin):
                fin_parts.append(pd.read_csv(p_fin))
            p_com = os.path.join(self.dir, f"com_{y}.csv")
            if not os.path.exists(p_com) and self.online:
                try:
                    z = zipfile.ZipFile(io.BytesIO(_http(COT_COM_URL.format(year=y))))
                    name = z.namelist()[0]
                    pd.read_csv(io.StringIO(z.read(name).decode("latin-1"))).to_csv(p_com, index=False)
                except Exception as e:
                    self._notes.append(f"COT-com {y}: {type(e).__name__}")
            if os.path.exists(p_com):
                com_parts.append(pd.read_csv(p_com))
        if fin_parts:
            f = pd.concat(fin_parts, ignore_index=True)
            f["dt"] = pd.to_datetime(f["Report_Date_as_YYYY-MM-DD"], errors="coerce")
            self._fin_df = f.dropna(subset=["dt"]).sort_values("dt").reset_index(drop=True)
        if com_parts:
            c = pd.concat(com_parts, ignore_index=True)
            c["dt"] = pd.to_datetime(c["As of Date in Form YYYY-MM-DD"], errors="coerce")
            # فرمت legacy نام ستون با فاصله دارد؛ ستون‌ها را عمومی پیدا می‌کنیم
            code_col = next(cc for cc in c.columns
                            if "contract market code" in cc.lower().replace("_", " "))
            l_col = next(cc for cc in c.columns if "noncommercial positions-long (all)" in cc.lower())
            s_col = next(cc for cc in c.columns if "noncommercial positions-short (all)" in cc.lower())
            c = c.rename(columns={code_col: "code", l_col: "l", s_col: "s"})
            self._com_df = c.dropna(subset=["dt"]).sort_values("dt").reset_index(drop=True)
        return self._fin_df is not None or self._com_df is not None

    def cot_net(self, symbol: str, now: pd.Timestamp) -> dict | None:
        """خالص پوزیشن هفتگی ≤ now + صدک ۳ سال اخیر. برمی‌گرداند: net, pctile, asof, n."""
        now = pd.Timestamp(now).tz_localize(None) if pd.Timestamp(now).tzinfo else pd.Timestamp(now)
        if not self._ensure_cot():
            return None
        if symbol in COT_FIN_CODES:
            code = COT_FIN_CODES[symbol]
            f = self._fin_df[self._fin_df["CFTC_Contract_Market_Code"].astype(str).str.zfill(6) == code]
            if f.empty:
                return None
            net_series = (f[FIN_POS_COLS[0]] - f[FIN_POS_COLS[1]]
                          + f[FIN_POS_COLS[2]] - f[FIN_POS_COLS[3]])
            src = "CME financial (AM+LF)"
        elif symbol in COT_COM_CODES:
            code = COT_COM_CODES[symbol]
            f = self._com_df[self._com_df["code"].astype(str).str.zfill(6) == code]
            if f.empty:
                return None
            net_series = f["l"] - f["s"]
            src = "COMEX legacy (noncommercial)"
        else:
            return None
        f2, net_series = f[f["dt"] <= now], net_series[f["dt"] <= now]
        if len(f2) < 40:
            return None
        net = float(net_series.iloc[-1])
        window = net_series.tail(156)          # ~۳ سال هفتگی
        pctile = float((window < net).mean())  # صدک موقعیت فعلی در بازه
        return {"net": net, "pctile": round(pctile, 3), "asof": str(f2["dt"].iloc[-1].date()),
                "n_weeks": int(len(f2)), "source": src}

    @property
    def notes(self) -> list:
        return list(self._notes)
