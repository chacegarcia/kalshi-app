namespace KalshiBotWrapper.Bot;

/// <summary>
/// Desired order intent. Mirrors Python strategy.TradeIntent.
/// </summary>
public sealed record TradeIntent(
    string Ticker,
    string Side,           // "yes" | "no"
    string Action,         // "buy" | "sell"
    int Count,
    int YesPriceCents,
    string TimeInForce = "good_till_canceled"
)
{
    public TradeIntent WithCount(int count) => this with { Count = count };
}

public static class TradeIntentHelper
{
    /// <summary>Net YES contracts added (long YES > 0, long NO < 0).</summary>
    public static double SignedPositionDelta(TradeIntent intent)
    {
        var c = (double)intent.Count;
        return (intent.Side, intent.Action) switch
        {
            ("yes", "buy") => c,
            ("yes", "sell") => -c,
            ("no", "buy") => -c,
            ("no", "sell") => c,
            _ => 0.0
        };
    }

    /// <summary>Absolute net position after the order (for per-market contract cap).</summary>
    public static double ProjectedAbsPositionAfter(double currentSignedPosition, TradeIntent intent)
        => Math.Abs(currentSignedPosition + SignedPositionDelta(intent));
}
