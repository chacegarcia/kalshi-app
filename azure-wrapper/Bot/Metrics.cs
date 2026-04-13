namespace KalshiBotWrapper.Bot;

public sealed record TradeOutcome(double PnlCents, double EdgeEstimateCents);

/// <summary>
/// Performance metrics for research. Mirrors Python metrics.py.
/// </summary>
public static class Metrics
{
    public const string NoGuaranteeDisclaimer =
        "These metrics are descriptive summaries of simulated or historical samples. " +
        "They do not guarantee future profitability or real-world fill quality.";

    public static double MaxDrawdown(IReadOnlyList<double> equityCents)
    {
        if (equityCents.Count == 0) return 0.0;
        var peak = equityCents[0];
        var maxDd = 0.0;
        foreach (var x in equityCents)
        {
            if (x > peak) peak = x;
            var dd = peak != 0 ? (x - peak) / peak : 0.0;
            if (dd < maxDd) maxDd = dd;
        }
        return maxDd;
    }

    public static double SharpeLike(IReadOnlyList<double> returns, double periodsPerYear = 252.0)
    {
        if (returns.Count < 2) return 0.0;
        var m = returns.Average();
        var variance = returns.Sum(r => (r - m) * (r - m)) / (returns.Count - 1);
        var std = variance > 0 ? Math.Sqrt(variance) : 1e-12;
        return (m / std) * Math.Sqrt(periodsPerYear);
    }

    public static double WinRate(IReadOnlyList<TradeOutcome> trades)
    {
        if (trades.Count == 0) return 0.0;
        return (double)trades.Count(t => t.PnlCents > 0) / trades.Count;
    }

    public static double AverageEdgeEstimate(IReadOnlyList<TradeOutcome> trades)
    {
        if (trades.Count == 0) return 0.0;
        return trades.Average(t => t.EdgeEstimateCents);
    }

    public static string FormatReport(IReadOnlyList<TradeOutcome> trades, IReadOnlyList<double> equityCents)
    {
        var rets = new List<double>();
        for (int i = 1; i < equityCents.Count; i++)
        {
            var a = equityCents[i - 1];
            var b = equityCents[i];
            if (a != 0) rets.Add((b - a) / Math.Abs(a));
        }
        return string.Join('\n',
            NoGuaranteeDisclaimer,
            $"Trades: {trades.Count}",
            $"Win rate: {WinRate(trades):P2}",
            $"Avg edge estimate (cents): {AverageEdgeEstimate(trades):F4}",
            $"Max drawdown (fraction): {MaxDrawdown(equityCents):F4}",
            $"Sharpe-like (returns-based): {SharpeLike(rets):F4}");
    }

    public static IEnumerable<(int TrainStart, int TrainEnd, int TestStart, int TestEnd)>
        WalkForwardIndices(int n, int nWindows, double trainRatio)
    {
        if (nWindows < 1 || n < 2) yield break;
        var window = Math.Max(1, n / nWindows);
        for (int w = 0; w < nWindows; w++)
        {
            var start = w * window;
            var end = Math.Min(n, start + window);
            if (end - start < 2) continue;
            var split = start + (int)((end - start) * trainRatio);
            if (split <= start || split >= end) continue;
            yield return (start, split, split, end);
        }
    }
}
