namespace KalshiBotWrapper.Bot;

/// <summary>
/// Scale max contracts and exposure caps from account balance. Mirrors Python sizing.py.
/// </summary>
public static class Sizing
{
    /// <summary>Cap order size: min(config max, balance × TRADE_RISK_PCT_OF_BALANCE_PER_TRADE / price).</summary>
    public static int EffectiveMaxContracts(TradingSettings settings, int? balanceCents, int yesPriceCents)
    {
        var baseMax = settings.MaxContractsPerMarket;
        if (!settings.TradeBalanceSizingEnabled || balanceCents is null or <= 0) return baseMax;
        var price = Math.Max(1, Math.Min(99, yesPriceCents));
        var budget = (double)balanceCents.Value * settings.TradeRiskPctOfBalancePerTrade;
        var cap = (int)(budget / price);
        return Math.Max(1, Math.Min(baseMax, cap));
    }

    /// <summary>Cap contracts for buy YES so approximate cash at limit stays ≤ max_notional_usd.</summary>
    public static int CapBuyYesCountForNotional(
        int count, int yesPriceCents, double? maxNotionalUsd, string side, string action)
    {
        if (side != "yes" || action != "buy") return count;
        if (maxNotionalUsd is null or <= 0) return count;
        var p = Math.Max(1, Math.Min(99, yesPriceCents)) / 100.0;
        var maxN = (int)(maxNotionalUsd.Value / p);
        return Math.Max(0, Math.Min(count, maxN));
    }

    /// <summary>Cap total exposure: min(MAX_EXPOSURE_CENTS, balance × TRADE_TOTAL_RISK_PCT_OF_BALANCE).</summary>
    public static double EffectiveMaxExposureCents(TradingSettings settings, int? balanceCents)
    {
        var staticMax = settings.MaxExposureCents;
        if (!settings.TradeBalanceSizingEnabled || balanceCents is null or <= 0) return staticMax;
        var scaled = (double)balanceCents.Value * settings.TradeTotalRiskPctOfBalance;
        return Math.Min(staticMax, scaled);
    }
}
