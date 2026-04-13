namespace KalshiBotWrapper.Bot;

/// <summary>
/// Simulated fills and paper portfolio. Mirrors Python paper_engine.py.
/// </summary>
public sealed class PaperFillConfig
{
    public double FillProbabilityIfCrossed { get; init; } = 0.85;
    public double PartialFillFraction { get; init; } = 1.0;
    public double FeeCentsPerContract { get; init; } = 0.0;
    public double SlippageCentsPerContract { get; init; } = 0.0;
    public bool Deterministic { get; init; } = false;
}

public sealed class PaperPortfolio
{
    public double CashCents { get; private set; }
    public double PositionContracts { get; private set; }
    public double AvgEntryCents { get; private set; }
    public List<double> EquityHistory { get; } = [];

    public PaperPortfolio(double initialCashCents = 0) => CashCents = initialCashCents;

    public void MarkEquity(double midCents)
        => EquityHistory.Add(CashCents + PositionContracts * midCents);

    public void ApplyBuyYes(double contracts, double priceCents, double feeSlippageCents)
    {
        var cost = contracts * priceCents + feeSlippageCents;
        CashCents -= cost;
        if (PositionContracts + contracts == 0)
            AvgEntryCents = 0;
        else
        {
            var tot = PositionContracts + contracts;
            AvgEntryCents = (AvgEntryCents * PositionContracts + priceCents * contracts) / tot;
        }
        PositionContracts += contracts;
    }
}

public static class PaperSimulator
{
    /// <summary>
    /// Returns (filled_contracts, effective_price_cents, edge_estimate_cents) or zeros if no fill.
    /// </summary>
    public static (double Filled, double EffectivePriceCents, double EdgeTotal)
        MatchLimitOrder(TradeIntent intent, double yesBidDollars, double yesAskDollars, PaperFillConfig cfg)
    {
        if (intent.Side != "yes" || intent.Action != "buy") return (0, 0, 0);

        var limitCents = (double)intent.YesPriceCents;
        var ask = yesAskDollars * 100.0;
        var bid = yesBidDollars * 100.0;
        var mid = (bid + ask) / 2.0;

        if (limitCents + 1e-9 < ask) return (0, 0, 0);

        var p = cfg.FillProbabilityIfCrossed;
        if (cfg.Deterministic)
            p = p >= 0.5 ? 1.0 : 0.0;
        else if (Random.Shared.NextDouble() > p)
            return (0, 0, 0);

        var filled = intent.Count * cfg.PartialFillFraction;
        var eff = limitCents + cfg.SlippageCentsPerContract;
        var edge = (mid - eff) * filled;
        return (filled, eff, edge);
    }

    /// <summary>Produce one TradeOutcome and fee cost for a hypothetical fill.</summary>
    public static (TradeOutcome? Outcome, double FeeCost)
        SimulateFill(TradeIntent intent, double yesBidDollars, double yesAskDollars, PaperFillConfig cfg)
    {
        var (filled, eff, edgeTotal) = MatchLimitOrder(intent, yesBidDollars, yesAskDollars, cfg);
        if (filled <= 0) return (null, 0);
        var feeCost = cfg.FeeCentsPerContract * filled;
        var pnl = edgeTotal - feeCost;
        return (new TradeOutcome(pnl, filled > 0 ? edgeTotal / filled : 0), feeCost);
    }
}
