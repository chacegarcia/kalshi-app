"""OpenAI helpers: fair value hints and full opportunity evaluation (OPENAI_API_KEY). Not financial advice."""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

from kalshi_bot.config import Settings

_log = logging.getLogger(__name__)


def _normalize_openai_message_content(content: Any) -> str:
    """Chat Completions message content may be a string or a list of blocks (some models)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and "text" in block:
                    parts.append(str(block["text"]))
                elif "text" in block:
                    parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts).strip()
    return str(content).strip()


def _model_uses_gpt5_style_chat_params(model: str) -> bool:
    """GPT-5 / o-series: ``max_completion_tokens`` (not ``max_tokens``), avoid non-default ``temperature``."""
    m = (model or "").lower()
    return "gpt-5" in m or m.startswith("o1") or m.startswith("o3") or m.startswith("o4")


def _parse_json_object_from_text(text: str) -> dict[str, Any] | None:
    """Parse first JSON object from model output (handles fences and leading prose)."""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) >= 2 else raw
        raw = raw.strip()
        if raw.lstrip().startswith("json"):
            raw = raw.lstrip()[4:].lstrip()
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw, start)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        return None
    return None


def _llm_approval_tail(settings: Settings) -> str:
    """Short tails to limit prompt tokens; code re-checks edge/fees."""
    fair_tail = ""
    if settings.trade_llm_accept_when_fair_covers_ask:
        fair_tail = (
            f" You may approve when fair_yes is within ~{settings.trade_llm_fair_ask_slippage:.2f} of implied ask (favorites)."
        )
    if settings.trade_llm_relaxed_approval:
        return (
            "Prefer approve=true when fair_yes ≥ implied ask or small edge; decline only junk/nonsense prices."
            + fair_tail
        )
    return (
        "Approve when fair_yes supports buying near ask; decline only obvious overpay or junk."
        + fair_tail
    )


@dataclass
class LLMOpportunityVerdict:
    approve: bool
    fair_yes: float
    buy_yes: bool
    limit_yes_price_cents: int
    contracts: int  # Kalshi API: YES contracts; trading-app synonym: shares
    reason: str

    @property
    def shares(self) -> int:
        return self.contracts


@dataclass
class LLMDiscoveryVerdict:
    """LLM only filters which tickers enter the rule-based pipeline (no buy/sell decision)."""

    watch: bool
    reason: str


def _bitcoin_market_context(ticker: str, title: str) -> bool:
    u = (ticker or "").upper()
    if u.startswith("KXBTC"):
        return True
    low = (title or "").lower()
    return "bitcoin" in low or " btc" in low or "btc " in low


def _llm_prompt_edge_settings(settings: Settings) -> tuple[float, float]:
    return _llm_prompt_edge_settings_with_adaptive(
        settings,
        adaptive_extra_min_net_edge=0.0,
        adaptive_extra_mid_edge=0.0,
    )


def _llm_prompt_edge_settings_with_adaptive(
    settings: Settings,
    *,
    adaptive_extra_min_net_edge: float = 0.0,
    adaptive_extra_mid_edge: float = 0.0,
) -> tuple[float, float]:
    mn = settings.trade_llm_min_net_edge_after_fees
    me = settings.trade_llm_edge_middle_extra_edge
    base_mn = settings.trade_min_net_edge_after_fees if mn is None else mn
    base_me = settings.trade_edge_middle_extra_edge if me is None else me
    return (base_mn + adaptive_extra_min_net_edge, base_me + adaptive_extra_mid_edge)


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
    adaptive_extra_min_net_edge: float = 0.0,
    adaptive_extra_mid_edge: float = 0.0,
    session_performance_note: str = "",
    tape_flow_usd_approx: float | None = None,
    tape_rank: int | None = None,
    tape_public_trade_count: int | None = None,
    tape_universe_size: int | None = None,
    no_bid_cents: int | None = None,
    no_ask_cents: int | None = None,
    no_bid_dollars: float | None = None,
    no_ask_dollars: float | None = None,
    entry_side: Literal["yes", "no"] = "yes",
) -> LLMOpportunityVerdict | None:
    """Ask the model to reason about a market; output must be JSON with ``shares`` (or legacy ``contracts``).

    When ``llm-trade --tape`` is used, pass tape fields so the model can weigh recent anonymous public flow
    (liquidity / attention proxy) alongside price and edge.
    """
    key = settings.openai_api_key
    if not key:
        return None

    bal_s = str(balance_cents) if balance_cents is not None else "?"
    min_e, mid_x = _llm_prompt_edge_settings_with_adaptive(
        settings,
        adaptive_extra_min_net_edge=adaptive_extra_min_net_edge,
        adaptive_extra_mid_edge=adaptive_extra_mid_edge,
    )
    min_chance = settings.trade_entry_effective_min_yes_ask_cents
    chance_line = (
        f"min_implied_yes_chance_pct={min_chance} (same as Kalshi 'chance' column; do not recommend buys below this implied %%), "
        if min_chance > 0
        else ""
    )
    mi_line = ""
    if settings.trade_entry_market_intelligence_enabled:
        mi_line = (
            f"market_intelligence: multi-outcome events (2+ open markets under same event) → only top "
            f"{settings.trade_entry_multi_choice_top_n} by implied YES and min ask "
            f"{settings.trade_entry_multi_choice_min_yes_ask_cents}¢; single-market events = binary. "
        )
    params = (
        f"Enforced in code: {chance_line}{mi_line}"
        f"min_net_edge_after_fees={min_e}, mid_price_extra_edge={mid_x}, "
        f"max_yes_ask={settings.strategy_max_yes_ask_dollars}, min_spread={settings.strategy_min_spread_dollars}, "
        f"max_shares={max_contracts_allowed}, bal_cents={bal_s}"
    )
    perf_block = ""
    if session_performance_note.strip():
        perf_block = f"Performance / risk note (session):\n{session_performance_note.strip()}\n\n"
    btc = _bitcoin_market_context(ticker, title)
    odds_block = (
        "This is a Bitcoin-tilted or BTC-series market: weigh spot/volatility and headline risk; you may size slightly "
        "higher only when fair_yes clearly exceeds the ask by a durable margin; otherwise favor consistency.\n"
        if btc
        else ""
    )
    style_block = (
        "Style: Treat each YES as a share of a $1 binary (max payoff $1). Share price = implied probability (the ask in ¢). "
        "Estimate fair_yes in [0,1]. The bot rejects trades that fail fee-aware edge vs mid. Prefer smaller `shares` "
        "when uncertain—many small wins, not home runs.\n"
    )
    flow_block = ""
    if (
        tape_flow_usd_approx is not None
        and tape_rank is not None
        and tape_public_trade_count is not None
        and tape_universe_size is not None
    ):
        flow_block = (
            "Tape context (anonymous public prints in this fetch window): "
            f"approx ${tape_flow_usd_approx:.2f} notional across {tape_public_trade_count} trade line(s); "
            f"flow rank #{tape_rank} of {tape_universe_size} in this scan. "
            "Higher flow often means more attention and easier exit liquidity, but it is not a guarantee of edge—"
            "still compare fair_yes to the ask and avoid chasing.\n\n"
        )

    no_line = ""
    if (
        no_bid_cents is not None
        and no_ask_cents is not None
        and no_bid_dollars is not None
        and no_ask_dollars is not None
    ):
        no_line = f"\nNO bid {no_bid_cents}¢ ask≈{no_ask_cents}¢ ({no_ask_dollars:.3f})."
    side_note = ""
    if entry_side == "no":
        side_note = (
            "\nExecution note: the bot will buy NO for this pass (higher implied lift vs YES). "
            "Estimate fair_yes as usual; fee-aware checks use fair_no = 1 − fair_yes vs the NO ask.\n"
        )

    user = f"""{ticker} | {title}
YES bid {yes_bid_cents}¢ ask≈{yes_ask_cents}¢ ({yes_ask_dollars:.3f}).{no_line}{side_note}
{flow_block}{perf_block}{params}
{style_block}{odds_block}JSON only:
{{"approve":bool,"fair_yes":0-1,"buy_yes":bool,"limit_yes_price_cents":1-99,"shares":1-{max_contracts_allowed},"reason":"brief"}}
{_llm_approval_tail(settings)}"""

    raw = _openai_chat_json(key, settings.trade_llm_model, user)
    if raw is None:
        return None
    try:
        approve = bool(raw.get("approve", False))
        fair_yes = float(raw.get("fair_yes", 0.5))
        buy_yes = bool(raw.get("buy_yes", False))
        limit_c = int(raw.get("limit_yes_price_cents", yes_ask_cents))
        raw_sz = raw.get("shares", raw.get("contracts", 1))
        contracts = int(raw_sz)
        reason = str(raw.get("reason", ""))[:2000]
        fair_yes = max(0.0, min(1.0, fair_yes))
        limit_c = max(1, min(99, limit_c))
        eff = settings.trade_entry_effective_min_yes_ask_cents
        clamp_ask = no_ask_cents if entry_side == "no" else yes_ask_cents
        if eff > 0:
            limit_c = max(limit_c, eff)
            limit_c = min(limit_c, clamp_ask)
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
    raw = _openai_chat_json_with_system(
        key, settings.trade_llm_model, system=system, user=user, temperature=0.15, max_tokens=220, json_mode=True
    )
    if raw is None:
        return None
    try:
        watch = bool(raw.get("watch", False))
        reason = str(raw.get("reason", ""))[:2000]
        return LLMDiscoveryVerdict(watch=watch, reason=reason)
    except (TypeError, ValueError):
        return None


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _openai_post_chat_completions(api_key: str, body: dict[str, Any]) -> dict[str, Any] | None:
    """POST /v1/chat/completions; log HTTP errors (common: wrong max_* or temperature for GPT-5)."""
    url = "https://api.openai.com/v1/chat/completions"
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
        with urllib.request.urlopen(req, timeout=90, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        _log.warning("openai_chat_completions_http_error status=%s body=%s", e.code, err[:3000])
        return None
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        _log.warning("openai_chat_completions_request_error %s", e)
        return None


def _choice_message_to_parsed_json(raw: dict[str, Any]) -> dict[str, Any] | None:
    choice0 = (raw.get("choices") or [{}])[0]
    msg = choice0.get("message") or {}
    text = _normalize_openai_message_content(msg.get("content"))
    fr = choice0.get("finish_reason")
    if not text:
        _log.warning(
            "openai_empty_assistant_content finish_reason=%s message_keys=%s",
            fr,
            list(msg.keys()),
        )
        return None
    out = _parse_json_object_from_text(text)
    if out is None:
        _log.warning("openai_json_parse_failed snippet=%s", text[:800])
    return out


def _openai_chat_json_with_system(
    api_key: str,
    model: str,
    *,
    system: str,
    user: str,
    temperature: float = 0.15,
    max_tokens: int | None = None,
    json_mode: bool = True,
) -> dict[str, Any] | None:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    max_out = max_tokens if max_tokens is not None else 512
    body: dict[str, Any] = {"model": model, "messages": messages}

    if _model_uses_gpt5_style_chat_params(model):
        # Reasoning models consume output budget before visible text; GPT-5 rejects max_tokens / low custom temperature.
        body["max_completion_tokens"] = max(max_out, 2048)
        body["reasoning_effort"] = "low"
    else:
        body["max_tokens"] = max_out
        body["temperature"] = temperature

    if json_mode:
        body["response_format"] = {"type": "json_object"}

    raw = _openai_post_chat_completions(api_key, body)
    if raw is None and "reasoning_effort" in body:
        body = {k: v for k, v in body.items() if k != "reasoning_effort"}
        raw = _openai_post_chat_completions(api_key, body)

    if raw is None:
        return None

    parsed = _choice_message_to_parsed_json(raw)
    if parsed is not None:
        return parsed

    if json_mode and _model_uses_gpt5_style_chat_params(model):
        body2 = {k: v for k, v in body.items() if k != "response_format"}
        raw2 = _openai_post_chat_completions(api_key, body2)
        if raw2:
            return _choice_message_to_parsed_json(raw2)
    return None


def _openai_chat_json(api_key: str, model: str, user_content: str) -> dict[str, Any] | None:
    return _openai_chat_json_with_system(
        api_key,
        model,
        system=(
            "Kalshi binary markets: title + bid/ask only. Prefer approve when fair_yes fits the ask or small edge; "
            "strict JSON only."
        ),
        user=user_content,
        temperature=0.22,
        max_tokens=512,
        json_mode=True,
    )


def _openai_json_fair_only(api_key: str, model: str, title: str, ticker: str) -> float | None:
    messages = [
        {
            "role": "system",
            "content": (
                "You estimate fair probability P(YES) for Kalshi binary markets. "
                'Output ONLY valid JSON: {"fair_yes":0.55} with fair_yes between 0 and 1.'
            ),
        },
        {
            "role": "user",
            "content": f"Market ticker: {ticker}\nTitle: {title}\nReturn JSON only.",
        },
    ]
    body: dict[str, Any] = {"model": model, "messages": messages, "response_format": {"type": "json_object"}}
    if _model_uses_gpt5_style_chat_params(model):
        body["max_completion_tokens"] = 256
        body["reasoning_effort"] = "low"
    else:
        body["temperature"] = 0.2
        body["max_tokens"] = 40
    raw = _openai_post_chat_completions(api_key, body)
    if raw is None:
        return None
    parsed = _choice_message_to_parsed_json(raw)
    if parsed is None:
        return None
    try:
        fy = float(parsed.get("fair_yes", 0.5))
        return max(0.0, min(1.0, fy))
    except (TypeError, ValueError):
        return None
