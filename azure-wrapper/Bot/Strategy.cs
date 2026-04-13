using System.Text.Json;

namespace KalshiBotWrapper.Bot;

/// <summary>
/// Strategy signal functions. Mirrors Python strategy.py.
/// </summary>
public static class Strategy
{
    /// <summary>
    /// Shared rule logic for WebSocket tickers and backtest PriceRecord rows.
    /// Returns null if no signal.
    /// </summary>
    public static TradeIntent? SignalFromBar(
        string ticker,
        double yesBidDollars,
        double yesAskDollars,
        double maxYesAskDollars,
        double minSpreadDollars,
        double probabilityGap,
        int orderCount,
        int limitPriceCents)
    {
        var spread = Math.Max(0.0, yesAskDollars - yesBidDollars);
        if (spread < minSpreadDollars) return null;

        var mid = (yesBidDollars + yesAskDollars) / 2.0;
        if (Math.Abs(mid - 0.5) < probabilityGap) return null;

        if (yesAskDollars > maxYesAskDollars) return null;

        return new TradeIntent(ticker, "yes", "buy", orderCount, limitPriceCents);
    }

    /// <summary>
    /// Buy YES only if fair_yes − ask − taker fee clears a mid-aware minimum edge.
    /// Returns null if no signal.
    /// </summary>
    public static TradeIntent? SignalEdgeBuyYesFromTicker(
        string ticker,
        double yesBidDollars,
        double yesAskDollars,
        TradingSettings settings)
    {
        var spread = Math.Max(0.0, yesAskDollars - yesBidDollars);
        if (spread < settings.StrategyMinSpreadDollars) return null;

        if (settings.TradeFairYesProb is null) return null;
        var fair = settings.TradeFairYesProb.Value;

        var mid = (yesBidDollars + yesAskDollars) / 2.0;
        var c = settings.StrategyOrderCount;
        var edge = EdgeMath.NetEdgeBuyYesLong(fair, yesAskDollars, c);
        var need = EdgeMath.MinEdgeThresholdForMid(mid,
            settings.TradeMinNetEdgeAfterFees, settings.TradeEdgeMiddleExtraEdge);

        if (edge < need) return null;
        if (yesAskDollars > settings.StrategyMaxYesAskDollars) return null;

        var limitCents = (int)Math.Max(1, Math.Min(99, Math.Round(yesAskDollars * 100.0)));
        return new TradeIntent(ticker, "yes", "buy", c, limitCents);
    }
}

/// <summary>
/// Research sample strategy: require min spread + probability gap + YES ask cap.
/// Mirrors Python SampleSpreadGapStrategy.
/// </summary>
public sealed class SampleSpreadGapStrategy
{
    private readonly TradingSettings _settings;

    public SampleSpreadGapStrategy(TradingSettings settings) => _settings = settings;

    public TradeIntent? OnTickerMessage(JsonElement message)
    {
        if (!message.TryGetProperty("type", out var typeEl)) return null;
        if (typeEl.GetString() != "ticker") return null;

        JsonElement body;
        if (!message.TryGetProperty("msg", out body)) return null;

        string? ticker = null;
        if (body.TryGetProperty("market_ticker", out var mt)) ticker = mt.GetString();
        else if (body.TryGetProperty("ticker", out var t)) ticker = t.GetString();

        if (string.IsNullOrEmpty(ticker)) return null;
        if (ticker != _settings.StrategyMarketTicker) return null;

        if (!TryGetDouble(body, "yes_bid_dollars", out var bid)) return null;
        if (!TryGetDouble(body, "yes_ask_dollars", out var ask)) return null;

        if (_settings.TradeUseEdgeStrategy && _settings.TradeFairYesProb.HasValue)
            return Strategy.SignalEdgeBuyYesFromTicker(ticker, bid, ask, _settings);

        return Strategy.SignalFromBar(
            ticker, bid, ask,
            _settings.StrategyMaxYesAskDollars,
            _settings.StrategyMinSpreadDollars,
            _settings.StrategyProbabilityGap,
            _settings.StrategyOrderCount,
            _settings.StrategyLimitPriceCents);
    }

    private static bool TryGetDouble(JsonElement el, string name, out double val)
    {
        if (el.TryGetProperty(name, out var prop) &&
            prop.ValueKind == JsonValueKind.Number)
        {
            val = prop.GetDouble();
            return true;
        }
        val = 0;
        return false;
    }
}
