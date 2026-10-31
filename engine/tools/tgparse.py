"""
تجزیه‌ی دستورهای تلگرام (خالص، بدون I/O).

ویژگی‌ها: ارقام فارسی/عربی، ممیز فارسی، و قاعده‌ی قطعی برای ویرگول:
  قیمت‌ها: ویرگول همیشه ممیز اعشار است (۱,۰۹۸ = 1.098)
  مبلغ (موجودی): ویرگول جداکننده‌ی هزارگان است (10,000 = 10000)
تا هیچ ورودی مبهمی بی‌صدا به حجم غلط تبدیل نشود.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional


class CommandError(ValueError):
    """خطای قابل‌نمایش به کاربر (پیام فارسی)."""


@dataclass
class Parsed:
    cmd: str                      # نام دستور یا "none" (متن عادی) یا "unknown"
    args: dict = field(default_factory=dict)


_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_text(s: str) -> str:
    s = (s or "").translate(_DIGITS).replace("٫", ".").replace("٬", ",").replace("،", ",")
    s = s.replace("\u200c", " ").replace("\u200f", "").replace("\u200e", "")
    return " ".join(s.split())


_THOUSANDS_RE = re.compile(r"\d{1,3}(,\d{3})+$")


def to_float(tok: str, comma: str = "decimal") -> float:
    """comma='decimal' برای قیمت؛ comma='thousands' برای مبلغ.

    برای قیمت، ویرگولِ هزارگان (مثل 2,400) عمداً رد می‌شود: قبلاً 2,400
    بی‌سروصدا 2.4 تفسیر می‌شد و حجم محاسبه‌شده صدها برابر اشتباه بود.
    کاربر باید صریح بنویسد: 2400 (عدد) یا 2400.50 / 2400,50 (اعشار)."""
    t = tok.strip()
    if comma == "thousands":
        t = t.replace(",", "")
    else:
        if _THOUSANDS_RE.fullmatch(t):
            raise CommandError(
                f"عدد مبهم: «{tok}» — ویرگول هزارگان برای قیمت مجاز نیست. "
                f"بنویس: «{tok.replace(',', '')}» یا اعشار «{tok.replace(',', '.')}»"
            )
        if "," in t and "." in t:
            # فرمت ترکیبی: ویرگول هزارگان + نقطه اعشار (مثل 2,400.5)
            t = t.replace(",", "")
        else:
            t = t.replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        raise CommandError(f"عدد نامعتبر: «{tok}»")
    if not math.isfinite(v):
        raise CommandError(f"عدد نامعتبر: «{tok}»")
    return v


_SYM_ALIASES = {"GOLD": "XAUUSD", "XAU": "XAUUSD", "طلا": "XAUUSD", "سکه": "XAUUSD"}


def norm_symbol(tok: str) -> str:
    t = tok.strip().upper().replace("=X", "").replace("/", "")
    t = re.sub(r"[.#_-].*$", "", t)          # پسوند بروکر مثل .m
    t = _SYM_ALIASES.get(t, t)
    if not re.fullmatch(r"[A-Z]{6}", t):
        raise CommandError(f"نماد نامعتبر: «{tok}» (مثل EURUSD یا طلا)")
    return t


_SIDES = {"buy": "BUY", "b": "BUY", "long": "BUY", "خرید": "BUY",
          "sell": "SELL", "s": "SELL", "short": "SELL", "فروش": "SELL"}


def norm_side(tok: str) -> str:
    v = _SIDES.get(tok.strip().lower())
    if not v:
        raise CommandError(f"جهت نامعتبر: «{tok}» (buy یا sell، یا خرید/فروش)")
    return v


_SET_KEYS = {"balance": "balance", "bal": "balance", "موجودی": "balance",
             "risk": "risk", "ریسک": "risk",
             "lev": "lev", "leverage": "lev", "اهرم": "lev",
             "maxloss": "maxloss", "maxtrades": "maxtrades", "consec": "consec"}

USAGE = {
    "size": "/size EURUSD 1.1000 1.0980 [TP] [risk=1 bal=10000 lev=100 jpy=155]",
    # jpy= نرخ USDJPY برای جفت‌های کراس ین (EURJPY/GBPJPY/AUDJPY) و gbp= نرخ GBPUSD برای EURGBP
    "log": "/log EURUSD buy 1.1000 1.0980 1.1040 [برچسب]",
    "set": "/set balance 10000  |  risk 1  |  lev 100 (یا off)  |  maxloss 3  |  maxtrades 5  |  consec 3",
    "last": "/last [تعداد]",
}


def _need(tokens, n, cmd):
    if len(tokens) < n:
        raise CommandError(f"ورودی ناقص. نمونه:\n{USAGE[cmd]}")


def _options(tokens):
    pos, opts = [], {}
    for t in tokens:
        if "=" in t:
            k, _, v = t.partition("=")
            opts[k.lower()] = v
        else:
            pos.append(t)
    return pos, opts


def parse_size(tokens: list) -> dict:
    pos, opts = _options(tokens)
    _need(pos, 3, "size")
    out = {"symbol": norm_symbol(pos[0]), "entry": to_float(pos[1]), "sl": to_float(pos[2]),
           "tp": to_float(pos[3]) if len(pos) > 3 else None}
    for k, v in opts.items():
        if k == "risk":
            out["risk"] = to_float(v)
        elif k in ("bal", "balance"):
            out["balance"] = to_float(v, "thousands")
        elif k in ("lev", "leverage"):
            out["lev"] = to_float(v)
        elif k in ("jpy", "gbp", "cad", "chf"):
            # نرخ ارز کوت به دلار برای محاسبه‌ی کراس‌ها (مثلاً jpy= نرخ USDJPY)
            out.setdefault("cross_rates", {})[k.upper()] = to_float(v)
        elif k == "tp":
            out["tp"] = to_float(v)
        else:
            raise CommandError(f"گزینه‌ی ناشناخته: «{k}»")
    return out


def parse_log(tokens: list) -> dict:
    _need(tokens, 5, "log")
    return {"symbol": norm_symbol(tokens[0]), "side": norm_side(tokens[1]),
            "entry": to_float(tokens[2]), "sl": to_float(tokens[3]), "exit": to_float(tokens[4]),
            "tag": " ".join(tokens[5:])[:40] or None}


def parse_set(tokens: list) -> dict:
    _need(tokens, 2, "set")
    key = _SET_KEYS.get(tokens[0].lower())
    if not key:
        raise CommandError(f"کلید ناشناخته: «{tokens[0]}»\n{USAGE['set']}")
    raw = tokens[1].lower()
    if key == "lev" and raw in ("off", "0", "خاموش"):
        return {"key": key, "value": None}
    v = to_float(tokens[1], "thousands" if key == "balance" else "decimal")
    if key == "balance" and v <= 0:
        raise CommandError("موجودی باید مثبت باشد")
    if key == "risk" and not (0 < v <= 100):
        raise CommandError("ریسک باید بین 0 و 100 باشد")
    if key == "lev" and not (0 < v <= 3000):
        raise CommandError("اهرم باید بین 0 و 3000 باشد")
    if key == "maxloss" and v <= 0:
        raise CommandError("maxloss باید مثبت باشد")
    if key in ("maxtrades", "consec"):
        if v < 1 or v != int(v):
            raise CommandError(f"{key} باید عدد صحیح ۱ یا بیشتر باشد")
        v = int(v)
    return {"key": key, "value": v}


def parse(text: str) -> Parsed:
    t = normalize_text(text)
    if not t.startswith("/"):
        return Parsed("none")
    head, *rest = t.split(" ")
    cmd = head[1:].split("@")[0].lower()
    if cmd in ("start", "help", "ping", "stats", "guard", "undo", "settings"):
        return Parsed("help" if cmd == "start" else cmd)
    if cmd == "size":
        return Parsed("size", parse_size(rest))
    if cmd == "log":
        return Parsed("log", parse_log(rest))
    if cmd == "set":
        return Parsed("set", parse_set(rest))
    if cmd == "last":
        n = 5
        if rest:
            n = int(to_float(rest[0]))
            if not 1 <= n <= 20:
                raise CommandError("تعداد باید بین ۱ و ۲۰ باشد")
        return Parsed("last", {"n": n})
    return Parsed("unknown", {"cmd": cmd})
