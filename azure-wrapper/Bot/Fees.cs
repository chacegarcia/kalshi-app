namespace KalshiBotWrapper.Bot;

/// <summary>
/// Kalshi general taker/maker fee formulas. Mirrors Python fees.py.
/// </summary>
public static class Fees
{
    private static double ClampPriceDollars(double p) => Math.Max(0.01, Math.Min(0.99, p));

    /// <summary>Taker fee in USD for the standard 7% coefficient schedule.</summary>
    public static double KalshiGeneralTakerFeeUsd(int contracts, double priceDollars)
    {
        var c = Math.Max(1, contracts);
        var p = ClampPriceDollars(priceDollars);
        var raw = 0.07 * c * p * (1.0 - p);
        return Math.Ceiling(raw * 100.0) / 100.0;
    }

    /// <summary>Maker fee in USD (1.75% coefficient).</summary>
    public static double KalshiGeneralMakerFeeUsd(int contracts, double priceDollars)
    {
        var c = Math.Max(1, contracts);
        var p = ClampPriceDollars(priceDollars);
        var raw = 0.0175 * c * p * (1.0 - p);
        return Math.Ceiling(raw * 100.0) / 100.0;
    }

    /// <summary>Average taker fee in USD for a 1-contract trade.</summary>
    public static double TakerFeePerContractUsd(double priceDollars)
        => KalshiGeneralTakerFeeUsd(1, priceDollars);

    /// <summary>Fee / price for a taker buy (rough intensity vs mid).</summary>
    public static double EffectiveFeeRateTaker(double priceDollars)
    {
        var p = ClampPriceDollars(priceDollars);
        return p <= 0 ? 0.0 : TakerFeePerContractUsd(p) / p;
    }
}
