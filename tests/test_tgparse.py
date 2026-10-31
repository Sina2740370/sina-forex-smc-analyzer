import pytest

from engine.tools.tgparse import CommandError, normalize_text, parse, to_float


def test_persian_digits_and_separators():
    assert normalize_text("۱٫۱۰۰۰ ۱٫۰۹۸۰") == "1.1000 1.0980"
    # قیمت با اعشار ویرگولی که مبهم نباشد قبول است (گروه بعد از ویرگول ≠ ۳ رقم)
    assert to_float("1,0980") == pytest.approx(1.098)              # قیمت: ویرگول = اعشار
    assert to_float("2,400.5") == pytest.approx(2400.5)
    assert to_float("10,000", "thousands") == 10000                # مبلغ: ویرگول = هزارگان


def test_ambiguous_thousands_comma_in_price_rejected():
    """2,400 / 1,098 مبهم‌اند (هزارگان یا اعشار؟) — باید خطای واضح بدهند،
    نه اینکه بی‌سروصدا 2.4 تفسیر شوند (قبلاً خطای ۱۰۰۰ برابری حجم می‌ساخت)."""
    for tok in ("2,400", "1,098", "1,234,567"):
        with pytest.raises(CommandError):
            to_float(tok)


def test_size_basic_and_options():
    p = parse("/size EURUSD 1.1000 1.0980 1.1040 risk=0.5 bal=10,000 lev=200")
    assert p.cmd == "size" and p.args["symbol"] == "EURUSD" and p.args["tp"] == 1.104
    assert p.args["risk"] == 0.5 and p.args["balance"] == 10000 and p.args["lev"] == 200


def test_size_persian_input_and_gold_alias():
    p = parse("/size طلا ۲۰۰۰٫۵ ۱۹۹۵")
    assert p.args["symbol"] == "XAUUSD" and p.args["entry"] == 2000.5 and p.args["sl"] == 1995


def test_symbol_suffix_and_errors():
    assert parse("/size eurusd.m 1.1 1.09").args["symbol"] == "EURUSD"
    with pytest.raises(CommandError):
        parse("/size EUR 1.1 1.09")
    with pytest.raises(CommandError):
        parse("/size EURUSD 1.1")                                  # ناقص
    with pytest.raises(CommandError):
        parse("/size EURUSD 1.1 abc")
    with pytest.raises(CommandError):
        parse("/size EURUSD 1.1 1.09 foo=1")


def test_log_and_sides():
    p = parse("/log EURUSD خرید 1.1 1.09 1.12 OB لندن")
    assert p.args == {"symbol": "EURUSD", "side": "BUY", "entry": 1.1, "sl": 1.09, "exit": 1.12,
                      "tag": "OB لندن"}
    assert parse("/log gbpusd s 1.3 1.31 1.29").args["side"] == "SELL"
    with pytest.raises(CommandError):
        parse("/log EURUSD up 1.1 1.09 1.12")


def test_set_rules():
    assert parse("/set balance 10,000").args == {"key": "balance", "value": 10000}
    assert parse("/set lev off").args == {"key": "lev", "value": None}
    assert parse("/set maxtrades 4").args == {"key": "maxtrades", "value": 4}
    for bad in ("/set risk 0", "/set risk 150", "/set maxtrades 2.5", "/set foo 1", "/set lev -3"):
        with pytest.raises(CommandError):
            parse(bad)


def test_misc_commands():
    assert parse("hello").cmd == "none"
    assert parse("/start").cmd == "help" and parse("/stats@MyBot").cmd == "stats"
    assert parse("/last 7").args == {"n": 7} and parse("/last").args == {"n": 5}
    assert parse("/whatever").cmd == "unknown"
    with pytest.raises(CommandError):
        parse("/last 99")
