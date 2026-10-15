"""
ارسال Notification به تلگرام
"""
import logging
import httpx

from .config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    ACCOUNT_BALANCE, LOT_RISK_PCT, MAX_ENTRY_SLIP_R,
)
from .position_sizing import broker_symbol, calc_lots

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


async def send_telegram(text: str, parse_mode: str = "HTML") -> bool:
    """ارسال پیام به تلگرام"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("تلگرام تنظیم نشده - پیام ارسال نشد")
        return False

    url = TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
        logger.info("📤 پیام تلگرام ارسال شد")
        return True
    except Exception as e:
        logger.error(f"خطا در ارسال تلگرام: {e}")
        return False


def _format_price(price, symbol: str) -> str:
    """فرمت قیمت بر اساس نماد"""
    if price is None:
        return "-"
    if symbol == "GC=F":
        return f"{price:.2f}"
    if "JPY" in symbol:
        return f"{price:.3f}"
    return f"{price:.5f}"


async def notify_new_signal(symbol: str, name: str, direction: str,
                              entry: float, sl: float, tp: float,
                              score: float, reasons: list,
                              trend: str = "-", zone: str = "-",
                              rates: dict = None):
    """اطلاع سیگنال جدید"""
    emoji = "🟢" if direction == "BUY" else "🔴"
    dir_fa = "خرید" if direction == "BUY" else "فروش"

    if direction == "BUY":
        risk = abs(entry - sl)
        reward = abs(tp - entry)
    else:
        risk = abs(sl - entry)
        reward = abs(entry - tp)

    rr = reward / risk if risk > 0 else 0

    # GC=F futures prices are not interchangeable with XAUUSD spot broker prices.
    # Keep the research signal visible, but never generate a spot-MT5 order recipe.
    if symbol == "GC=F":
        lot_line = "⛔ حجم قابل محاسبه نیست؛ قرارداد و کارمزد فیوچرز باید جداگانه مشخص شود.\n"
        mt5_block = (
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⛔ <b>برای XAUUSD اسپات در MT5 استفاده نکن</b>\n"
            "این تحلیل از GC=F (قرارداد آتی طلا) آمده است؛ قیمت ورود، SL و TP "
            "ممکن است با قیمت اسپات بروکر اختلاف داشته باشند.\n"
        )
    else:
        max_entry = entry + risk * MAX_ENTRY_SLIP_R if direction == "BUY" else entry - risk * MAX_ENTRY_SLIP_R
        lot = calc_lots(symbol, entry, sl, ACCOUNT_BALANCE, LOT_RISK_PCT, rates)
        if lot:
            lot_line = (
                f"📦 حجم: <b>{lot['lots']:.2f} لات</b>\n"
                f"    ریسک ≈ ${lot['risk_usd']:.2f} ({lot['risk_pct']:.2f}٪ از ${ACCOUNT_BALANCE:,.0f})\n"
            )
            if lot["over"]:
                lot_line += f"    ⚠️ حداقل لات ۰.۰۱ ریسک رو از {lot['target_pct']:.2f}٪ بیشتر می‌کنه\n"
                if lot["risk_pct"] > lot["target_pct"] * 2:
                    lot_line += "    ⛔ ریسک برای این حساب خیلی زیاده؛ این سیگنال رو رد کن\n"
        else:
            lot_line = "📦 حجم: قابل محاسبه نبود (دستی حساب نکن تا مشخصات بروکر بررسی شود)\n"
        mt5_block = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>ثبت در MT5</b>\n"
            f"🏷 نماد: <code>{broker_symbol(symbol)}</code>\n"
            f"{lot_line}"
            f"⏱ ورود فقط تا <code>{_format_price(max_entry, symbol)}</code>؛ بعدش (یا بعد ۱۵ دقیقه) ردش کن\n"
            f"🛑 SL و TP رو همون لحظه‌ی ثبت بذار\n"
        )

    text = (
        f"{emoji} <b>سیگنال {dir_fa}</b> {emoji}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>{name}</b>  (<code>{symbol}</code>)\n"
        f"💰 ورود: <code>{_format_price(entry, symbol)}</code>\n"
        f"🛑 حد ضرر: <code>{_format_price(sl, symbol)}</code>\n"
        f"🎯 حد سود: <code>{_format_price(tp, symbol)}</code>\n"
        f"⚖️ R:R = <b>1:{rr:.2f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 روند: <b>{trend}</b>\n"
        f"📍 ناحیه: <b>{zone}</b>\n"
        f"🎯 امتیاز: <b>{score:+.2f}</b>\n"
        f"{mt5_block}"
    )

    if reasons:
        text += "━━━━━━━━━━━━━━━━━━━━\n"
        text += "✅ <b>دلایل:</b>\n"
        for r in reasons[:6]:
            text += f"  • {r}\n"

    text += "\n🚀 <i>Sina Quant</i>"

    await send_telegram(text)


async def notify_signal_closed(symbol: str, name: str,
                                 direction: str, status: str,
                                 pnl_pips: float, entry: float,
                                 exit_price: float):
    """اطلاع بسته شدن سیگنال (WIN یا LOSS)"""
    if status == "WIN":
        emoji = "✅"
        title = "برد"
    else:
        emoji = "❌"
        title = "باخت"

    sign = "+" if pnl_pips > 0 else ""

    text = (
        f"{emoji} <b>{title}</b> {emoji}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>{name}</b>  (<code>{symbol}</code>)\n"
        f"🎯 جهت: <b>{direction}</b>\n"
        f"💰 ورود: <code>{_format_price(entry, symbol)}</code>\n"
        f"🏁 خروج: <code>{_format_price(exit_price, symbol)}</code>\n"
        f"📊 نتیجه: <b>{sign}{pnl_pips:.1f} پیپ</b>\n"
        f"\n🚀 <i>Sina Quant</i>"
    )

    await send_telegram(text)


async def notify_startup():
    """پیام تست/شروع"""
    text = (
        "🚀 <b>Sina Quant</b> روشن شد!\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ سیستم Notification فعاله\n"
        "📊 هر سیگنال جدید → اینجا پیام میاد\n"
        "🎯 هر WIN/LOSS → اینجا پیام میاد\n"
        "\n💚 موفق باشی!"
    )
    await send_telegram(text)
