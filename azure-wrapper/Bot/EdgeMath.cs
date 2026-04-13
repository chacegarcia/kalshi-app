namespace KalshiBotWrapper.Bot;

/// <summary>
/// Fee-adjusted edge and intra-market boxed (YES+NO) surplus. Mirrors Python edge_math.py.
/// </summary>
public static class EdgeMath
{
    private static double Clamp01(double x) => Math.Max(0.0, Math.Min(1.0, x));

    public static double ImpliedYesAskDollars(double bestNoBidDollars)
        => Math.Max(0.01, Math.Min(0.99, 1.0 - Clamp01(bestNoBidDollars)));

    public static double ImpliedNoAskDollars(double bestYesBidDollars)
        => Math.Max(0.01, Math.Min(0.99, 1.0 - Clamp01(bestYesBidDollars)));

    public static double BoxedPairCostDollars(double bestYesBidDollars, double bestNoBidDollars)
        => ImpliedYesAskDollars(bestNoBidDollars) + ImpliedNoAskDollars(bestYesBidDollars);

    public static double BoxedArbSurplusBeforeFeesdDollars(double bestYesBidDollars, double bestNoBidDollars)
        => 1.0 - BoxedPairCostDollars(bestYesBidDollars, bestNoBidDollars);

    public static double BoxedArbSurplusAfterTakerFeesDollars(
        double bestYesBidDollars, double bestNoBidDollars, int contracts = 1)
    {
        var ya = ImpliedYesAskDollars(bestNoBidDollars);
        var na = ImpliedNoAskDollars(bestYesBidDollars);
        var pay = ya + na;
        var fy = Fees.KalshiGeneralTakerFeeUsd(contracts, ya);
        var fn = Fees.KalshiGeneralTakerFeeUsd(contracts, na);
        return 1.0 - pay - (fy + fn) / Math.Max(1, contracts);
    }

    /// <summary>fair − ask − taker fee per contract (all in dollars). Conservative long YES signal.</summary>
    public static double NetEdgeBuyYesLong(double fairYes, double yesAskDollars, int contracts = 1)
    {
        var fy = Fees.KalshiGeneralTakerFeeUsd(contracts, yesAskDollars);
        var per = fy / Math.Max(1, contracts);
        return fairYes - yesAskDollars - per;
    }

    /// <summary>Extra edge required near 0.50 (fees worst at mid).</summary>
    public static double MiddlePenaltyMultiplier(double mid, double width = 0.15)
    {
        var d = Math.Abs(mid - 0.5);
        if (d >= width) return 0.0;
        return width - d;
    }

    /// <summary>Require larger edge near 50% (fee + adverse selection heuristic).</summary>
    public static double MinEdgeThresholdForMid(
        double mid,
        double baseMinEdge,
        double middleExtra,
        double middleWidth = 0.15)
    {
        return baseMinEdge + middleExtra *
            (MiddlePenaltyMultiplier(mid, middleWidth) / Math.Max(middleWidth, 1e-9));
    }
}
