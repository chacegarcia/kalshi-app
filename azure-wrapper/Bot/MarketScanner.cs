using KalshiBotWrapper.Kalshi;

namespace KalshiBotWrapper.Bot;

public sealed record ScanRow(
    string Ticker,
    string Title,
    int? YesBidCents,
    int? NoBidCents,
    double? BoxedSurplusBeforeFees,
    double? BoxedSurplusAfterFees,
    double? EdgeBuyYes);

/// <summary>
/// Scans open Kalshi markets for boxed surplus and directional edge. Mirrors Python scanner.py.
/// </summary>
public static class MarketScanner
{
    public static async Task<List<ScanRow>> ScanKalshiOpportunitiesAsync(
        KalshiRestClient client,
        TradingSettings settings,
        int limit = 40,
        CancellationToken ct = default)
    {
        var resp = await MarketData.ListOpenMarketsAsync(client, limit, "exclude", ct);
        var out_ = new List<ScanRow>();

        foreach (var m in resp.Markets)
        {
            GetOrderbookResponse ob;
            try { ob = await MarketData.GetOrderbookAsync(client, m.Ticker, ct); }
            catch { continue; }

            var yb = MarketData.BestYesBidCents(ob);
            var nb = MarketData.BestNoBidCents(ob);
            var ybd = yb.HasValue ? (double?)yb.Value / 100.0 : null;
            var nbd = nb.HasValue ? (double?)nb.Value / 100.0 : null;

            double? boxedBefore = null, boxedAfter = null;
            if (ybd.HasValue && nbd.HasValue)
            {
                boxedBefore = EdgeMath.BoxedArbSurplusBeforeFeesdDollars(ybd.Value, nbd.Value);
                boxedAfter = EdgeMath.BoxedArbSurplusAfterTakerFeesDollars(ybd.Value, nbd.Value, 1);
            }

            double? edgeBuy = null;
            if (settings.TradeFairYesProb.HasValue && ybd.HasValue && nbd.HasValue)
            {
                var ya = EdgeMath.ImpliedYesAskDollars(nbd.Value);
                edgeBuy = EdgeMath.NetEdgeBuyYesLong(
                    settings.TradeFairYesProb.Value, ya, settings.StrategyOrderCount);
            }

            out_.Add(new ScanRow(
                m.Ticker,
                m.Title.Length > 120 ? m.Title[..120] : m.Title,
                yb, nb, boxedBefore, boxedAfter, edgeBuy));
        }

        out_.Sort((a, b) =>
        {
            var ba = -(a.BoxedSurplusAfterFees ?? -1e9);
            var bb = -(b.BoxedSurplusAfterFees ?? -1e9);
            var diff = ba.CompareTo(bb);
            if (diff != 0) return diff;
            return (-(a.EdgeBuyYes ?? -1e9)).CompareTo(-(b.EdgeBuyYes ?? -1e9));
        });
        return out_;
    }

    public static string FormatScanReport(
        IReadOnlyList<ScanRow> rows, double minBoxedAfter = -1.0, double minEdge = -1.0)
    {
        var lines = new List<string>
        {
            "ticker\tboxed$_after_fees\tedge_vs_fair\tY_bid\tN_bid\ttitle",
            new string('—', 100),
        };
        var useFilter = minBoxedAfter >= 0.0 || minEdge >= 0.0;
        foreach (var r in rows)
        {
            if (useFilter)
            {
                if ((r.BoxedSurplusAfterFees ?? 0) < minBoxedAfter &&
                    (r.EdgeBuyYes ?? 0) < minEdge) continue;
            }
            var title = r.Title.Length > 60 ? r.Title[..60] : r.Title;
            lines.Add(
                $"{r.Ticker}\t{r.BoxedSurplusAfterFees ?? 0:F4}\t{r.EdgeBuyYes ?? 0:F4}\t{r.YesBidCents}\t{r.NoBidCents}\t{title}");
        }
        return string.Join('\n', lines);
    }
}
