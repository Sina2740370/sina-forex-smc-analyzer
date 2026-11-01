"""
قراردادهای داده‌ای MAS: هر ایجنت فقط AgentReport تحویل می‌دهد و هر سیگنال
فقط بعد از عبور از گیت‌های مستقل صادر می‌شود.

قواعد سخت پیاده‌شده اینجا:
- هیچ ایجنتی حق ندارد سیگنال نهایی بدهد؛ خروجی‌اش فقط گزارش است.
- هر دلیل (Reason) باید evidence داشته باشد (ارجاع به داده واقعی) — وگرنه اعتبارسنج رد می‌کند.
- داده غایب → missing_data پر می‌شود؛ حدس ممنوع.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Optional

BUY, SELL, NEUTRAL = "BUY", "SELL", "NEUTRAL"
DIRS = (BUY, SELL, NEUTRAL)


@dataclass
class Reason:
    id: str
    text: str
    evidence: str = ""


@dataclass
class AgentReport:
    agent: str                      # regime | technical | fundamental | sentiment | liquidity | debate
    direction: str = NEUTRAL
    score: float = 50.0             # 0..100 صعودی‌نگری: >50 تمایل خرید، <50 تمایل فروش
    confidence: float = 0.0         # 0..100
    reasons: list = field(default_factory=list)
    invalidation: list = field(default_factory=list)
    missing_data: list = field(default_factory=list)
    # زمینه اختیاری: سطوح/ستاپ‌های پیشنهادی برای ساخت کاندید معامله
    context: dict = field(default_factory=dict)
    is_stub: bool = False   # True = ایجنت غایب (داده ندارد) → در تجمیع رأی نمی‌دهد

    def validate(self) -> list:
        errs = []
        if self.agent not in ("regime", "technical", "fundamental", "sentiment",
                              "liquidity", "debate"):
            errs.append(f"agent نامعتبر: {self.agent}")
        if self.direction not in DIRS:
            errs.append(f"direction نامعتبر: {self.direction}")
        if not (0 <= self.score <= 100 and 0 <= self.confidence <= 100):
            errs.append("score/confidence خارج از بازه 0..100")
        for r in self.reasons:
            if not getattr(r, "evidence", ""):
                errs.append(f"reason {getattr(r, 'id', '?')} بدون evidence")
        return errs

    def to_dict(self) -> dict:
        return {
            "agent": self.agent, "direction": self.direction,
            "score": round(self.score, 1), "confidence": round(self.confidence, 1),
            "reasons": [asdict(r) if isinstance(r, Reason) else r for r in self.reasons],
            "invalidation": list(self.invalidation),
            "missing_data": list(self.missing_data),
        }


@dataclass
class GateResult:
    gate: str
    passed: bool
    detail: str = ""


@dataclass
class Signal:
    """سیگنال نهایی با اثر انگشت (replayable)."""
    symbol: str
    timeframe: str = "15m"
    session: str = ""
    market_regime: str = ""
    signal: str = "WAIT"            # BUY | SELL | WAIT
    final_score: float = 0.0
    confidence: float = 0.0
    entry_zone: str = ""
    entry_trigger: str = ""
    stop_loss: str = ""
    take_profit_1: str = ""
    take_profit_2: str = ""
    risk_reward: str = ""
    lot_size: str = ""
    risk_amount: str = ""
    invalidation: list = field(default_factory=list)
    agent_scores: dict = field(default_factory=dict)
    risk_approved: bool = False
    warnings: list = field(default_factory=list)
    wait_reason: str = ""           # شناسه گیت شکست‌خورده + دلیل
    agent_reports: list = field(default_factory=list)
    gate_results: list = field(default_factory=list)
    config_hash: str = ""
    missing_data: list = field(default_factory=list)

    def fingerprint(self, inputs_hash: str = "") -> str:
        payload = json.dumps({
            "symbol": self.symbol, "signal": self.signal, "ts": self.entry_zone,
            "final_score": self.final_score, "inputs": inputs_hash,
            "cfg": self.config_hash,
        }, sort_keys=True)
        return "sig_" + hashlib.sha256(payload.encode()).hexdigest()[:12]

    def to_json(self, inputs_hash: str = "", include_reports: bool = True) -> dict:
        d = {
            "symbol": self.symbol, "timeframe": self.timeframe, "session": self.session,
            "market_regime": self.market_regime, "signal": self.signal,
            "final_score": round(self.final_score, 1),
            "confidence": round(self.confidence, 1),
            "entry_zone": self.entry_zone, "entry_trigger": self.entry_trigger,
            "stop_loss": self.stop_loss, "take_profit_1": self.take_profit_1,
            "take_profit_2": self.take_profit_2, "risk_reward": self.risk_reward,
            "lot_size": self.lot_size, "risk_amount": self.risk_amount,
            "invalidation": self.invalidation, "agent_scores": self.agent_scores,
            "risk_approved": self.risk_approved, "warnings": self.warnings,
            "missing_data": self.missing_data, "wait_reason": self.wait_reason,
            "fingerprint": self.fingerprint(inputs_hash),
        }
        if include_reports:
            d["agent_reports"] = self.agent_reports
            d["gate_results"] = [asdict(g) for g in self.gate_results]
        return d


def missing_report(agent: str, what: list) -> AgentReport:
    """گزارش خنثی برای ایجنتی که داده‌اش در دسترس نیست (فاز بعدی وصل می‌شود).
    خنثی = score 50 / confidence 0 → در تجمیع فقط به‌سمت بی‌قطعیت‌شدن می‌رود، نه حدس."""
    return AgentReport(
        agent=agent, direction=NEUTRAL, score=50.0, confidence=0.0,
        missing_data=[f"{agent}: {w}" for w in what], is_stub=True,
        reasons=[Reason(id=f"{agent[0].upper()}-MD", text="داده در دسترس نیست؛ حدس زده نشد",
                        evidence=f"missing: {what}")],
    )


def data_hash(*frames) -> str:
    """هش قطعی از داده ورودی (برای Replay و اثر انگشت سیگنال)."""
    h = hashlib.sha256()
    for df in frames:
        if df is None or len(df) == 0:
            h.update(b"<empty>")
            continue
        h.update(str(len(df)).encode())
        h.update(str(round(float(df["close"].iloc[-1]), 5)).encode())
        h.update(str(df["time"].iloc[-1]).encode())
    return h.hexdigest()[:16]
