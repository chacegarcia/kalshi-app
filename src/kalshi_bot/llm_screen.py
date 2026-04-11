"""OpenAI helpers: fair value hints and full opportunity evaluation (OPENAI_API_KEY). Not financial advice."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from kalshi_bot.config import Settings


def _llm_approval_tail(settings: Settings) -> str:
    """Approval instructions: default = accumulate small wins; relaxed = even more permissive."""
    fair_tail = ""
    if settings.trade_llm_accept_when_fair_covers_ask:
        fair_tail = (
            f" You may also approve when fair_yes is within about {settings.trade_llm_fair_ask_slippage:.2f} of the implied "
            "YES ask (fairly priced favorites / high-confidence sides), not only when there is a large mispricing."
        )
    if settings.trade_llm_relaxed_approval:
        return (
            "Prefer approve=true and buy_yes=true when fair_yes is at or above the implied YES ask, or when fair_yes clears "
            "the min net edge after fees from the parameters, or when the ask looks even slightly cheap vs your fair_yes. "
            "Reserve decline for junk/empty titles, incoherent prices, or when YES is clearly overpriced vs fair_yes. "
            "Do not decline merely because the edge is small—small edges are the point. Never invent facts not in the title."
            + fair_tail
        )
    return (
        "Strategy: accumulate many small positive outcomes over time; the execution layer applies fee and risk checks. "
        "Set approve=true and buy_yes=true when fair_yes supports buying at or near the implied ask—including modest edge, "
        "fair value at the ask on favorites, or any reasonable case that is not an obvious overpay. "
        "Decline only for unusable titles, nonsense prices, or when YES is clearly worse than the ask. "
        "Bias toward approval when the setup is plausible; do not refuse good-enough trades in search of perfect mispricings. "
        "Never invent facts not in the title."
        + fair_tail
    )


@dataclass
class LLMOpportunityVerdict:
    approve: bool
    fair_yes: float
    buy_yes: bool
    limit_yes_price_cents: int
    contracts: int
    reason: str


@dataclass
class LLMDiscoveryVerdict:
    """LLM only filters which tickers enter the rule-based pipeline (no buy/sell decision)."""

    watch: bool
    reason: str


def optional_llm_fair_yes(title: str, *, ticker: str, settings: Settings) -> float | None:
    """Return model-estimated fair P(YES) in [0,1] or None if disabled / failure."""
    key = settings.openai_api_key
    if not key or not settings.trade_llm_screen_enabled:
        return None
    return _openai_json_fair_only(key, settings.trade_llm_model, title, ticker)


def llm_evaluate_opportunity(
    *,
    settings: Settings,
    ticker: str,
    title: str,
    yes_bid_cents: int,
    yes_ask_cents: int,
    yes_bid_dollars: float,
    yes_ask_dollars: float,
    balance_cents: int | None,
    max_contracts_allowed: int,
) -> LLMOpportunityVerdict | None:
    """Ask the model to reason about a market; output must be JSON matching ``LLMOpportunityVerdict`` shape."""
    key = settings.openai_api_key
    if not key:
        return None

    bal_s = str(balance_cents) if balance_cents is not None else "unknown"
    params = f"""Execution limits (the bot enforces these after your JSON; use them to guide fair_yes and approval, not to default to decline):
- Target min net edge after fees (0–1 scale on $1 face): {settings.trade_min_net_edge_after_fees}
- Extra edge suggested near 50% mid: {settings.trade_edge_middle_extra_edge}
- Max YES ask (dollars): {settings.strategy_max_yes_ask_dollars}
- Min spread (dollars): {settings.strategy_min_spread_dollars}
- Max contracts this order (balance-scaled): {max_contracts_allowed}
- Account balance (cents): {bal_s}
"""

    user = f"""Market ticker: {ticker}
Title: {title}
Order book (YES): bid={yes_bid_cents}¢ implied ask≈{yes_ask_cents}¢ ({yes_ask_dollars:.4f} dollars).

{params}

Decide on a long YES for an accumulation strategy (many small wins, not only blockbuster mispricings). Output ONLY valid JSON:
{{"approve": true/false, "fair_yes": 0.0-1.0, "buy_yes": true/false, "limit_yes_price_cents": 1-99, "contracts": 1-{max_contracts_allowed}, "reason": "short text"}}
{_llm_approval_tail(settings)}"""

    raw = _openai_chat_json(key, settings.trade_llm_model, user)
    if raw is None:
        return None
    try:
        approve = bool(raw.get("approve", False))
        fair_yes = float(raw.get("fair_yes", 0.5))
        buy_yes = bool(raw.get("buy_yes", False))
        limit_c = int(raw.get("limit_yes_price_cents", yes_ask_cents))
        contracts = int(raw.get("contracts", 1))
        reason = str(raw.get("reason", ""))[:2000]
        fair_yes = max(0.0, min(1.0, fair_yes))
        limit_c = max(1, min(99, limit_c))
        contracts = max(1, min(max_contracts_allowed, contracts))
        return LLMOpportunityVerdict(
            approve=approve,
            fair_yes=fair_yes,
            buy_yes=buy_yes,
            limit_yes_price_cents=limit_c,
            contracts=contracts,
            reason=reason,
        )
    except (TypeError, ValueError, KeyError):
        return None


def llm_discover_watchlist(settings: Settings, *, ticker: str, title: str) -> LLMDiscoveryVerdict | None:
    """True/false: should this market be passed to deterministic rules? Does not evaluate trades."""
    key = settings.openai_api_key
    if not key:
        return None
    q = (settings.trade_llm_discovery_query or "").strip()
    filter_block = (
        f"The user only wants markets that match this interest (be strict):\n{q}\n"
        if q
        else (
            "No specific theme: set watch=true for normal Kalshi prediction market titles; "
            "watch=false only for obvious junk, empty, or non-market text."
        )
    )
    user = f"""Market ticker: {ticker}
Title: {title}

{filter_block}

You do NOT decide whether to buy or sell. A separate program applies fixed math from the user's settings.
Output ONLY valid JSON: {{"watch": true/false, "reason": "short text"}}"""

    system = (
        "You filter Kalshi binary prediction markets for downstream rule-based software. "
        "You never give trading instructions. Output strict JSON only."
    )
    raw = _openai_chat_json_with_system(key, settings.trade_llm_model, system=system, user=user)
    if raw is None:
        return None
    try:
        watch = bool(raw.get("watch", False))
        reason = str(raw.get("reason", ""))[:2000]
        return LLMDiscoveryVerdict(watch=watch, reason=reason)
    except (TypeError, ValueError):
        return None


def _openai_chat_json_with_system(
    api_key: str, model: str, *, system: str, user: str, temperature: float = 0.15
) -> dict[str, Any] | None:
    url = "https://api.openai.com/v1/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        text = raw["choices"][0]["message"]["content"]
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) >= 2 else text
            if text.startswith("json"):
                text = text[4:].lstrip()
        return json.loads(text)
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError, TypeError, IndexError):
        return None


def _openai_chat_json(api_key: str, model: str, user_content: str) -> dict[str, Any] | None:
    return _openai_chat_json_with_system(
        api_key,
        model,
        system=(
            "You evaluate Kalshi binary prediction markets from title and top-of-book prices only. "
            "The operator wants to accumulate many small winning or fairly priced entries over time—bias toward approving "
            "when fair_yes aligns with or modestly exceeds the implied ask, not toward declining unless the case is weak. "
            "Output strict JSON only."
        ),
        user=user_content,
        temperature=0.22,
    )


def _openai_json_fair_only(api_key: str, model: str, title: str, ticker: str) -> float | None:
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You estimate fair probability P(YES) for Kalshi binary markets. "
                    "Output ONLY valid JSON: {\"fair_yes\":0.55} with fair_yes between 0 and 1."
                ),
            },
            {
                "role": "user",
                "content": f"Market ticker: {ticker}\nTitle: {title}\nReturn JSON only.",
            },
        ],
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        text = raw["choices"][0]["message"]["content"]
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text)
        fy = float(parsed.get("fair_yes", 0.5))
        return max(0.0, min(1.0, fy))
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None
