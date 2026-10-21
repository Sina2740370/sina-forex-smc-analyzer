"""
موتور تحلیل Top-Down SMC/ICT - نسخه 4.3
بر اساس جمع‌بندی استراتژی بهترین تریدرهای حرفه‌ای دنیا (ICT/SMC + اصول Paul Tudor Jones)

اصول اعمال‌شده:
1. Higher Timeframe Bias اولویت مطلق دارد
2. بهترین ستاپ = Liquidity Sweep → Structure Shift (CHOCH) → Entry در FVG/OB
3. Premium/Discount به صورت فیلتر نرم
4. کیفیت و confluence مهم‌تر از تعداد سیگنال
5. ریسک نامتقارن و محافظه‌کارانه حفظ شده
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import numpy as np

from .config import (
    MIN_CANDLES, MIN_SIGNAL_SCORE, RISK_REWARD,
    SL_ATR_MULTIPLIER, SL_OB_BUFFER,
    WEIGHT_TECHNICAL, WEIGHT_FUNDAMENTAL, WEIGHT_NEWS, WEIGHT_MEMORY,
    get_symbol_profile,
)

logger = logging.getLogger(__name__)

MIN_RISK_ATR_RATIO = 0.25
