using System.Text.Json;
using KalshiBotWrapper.Kalshi;

namespace KalshiBotWrapper.Bot;

public sealed record MarketSummary(string Ticker, string Title, string? Status);

/// <summary>
/// Market and orderbook REST helpers. Mirrors Python market_data.py.
/// </summary>
public static class MarketData
{
    public static async Task<GetMarketsResponse> ListOpenMarketsAsync(
        KalshiRestClient client, int limit = 50, string? mveFilter = "exclude", CancellationToken ct = default)
        => await client.GetMarketsAsync("open", limit, mveFilter, null, ct);

    public static async Task<GetOrderbookResponse> GetOrderbookAsync(
        KalshiRestClient client, string ticker, CancellationToken ct = default)
        => await client.GetOrderbookAsync(ticker, 10, ct);

    /// <summary>Best YES bid in cents from orderbook response. Null if no YES bids.</summary>
    public static int? BestYesBidCents(GetOrderbookResponse ob)
    {
        var best = BestBidDollars(ob.Orderbook?.Yes);
        if (best is null or <= 0) return null;
        return (int)Math.Round(best.Value * 100);
    }

    /// <summary>Best NO bid in cents from orderbook response. Null if no NO bids.</summary>
    public static int? BestNoBidCents(GetOrderbookResponse ob)
    {
        var best = BestBidDollars(ob.Orderbook?.No);
        if (best is null or <= 0) return null;
        return (int)Math.Round(best.Value * 100);
    }

    public static MarketSummary SummarizeMarket(Market m)
        => new(m.Ticker, m.Title, m.Status);

    private static double? BestBidDollars(List<List<JsonElement>>? levels)
    {
        if (levels is null || levels.Count == 0) return null;
        var best = 0.0;
        foreach (var row in levels)
        {
            if (row.Count >= 1 && row[0].ValueKind == JsonValueKind.Number)
                best = Math.Max(best, row[0].GetDouble());
        }
        return best > 0 ? best : null;
    }
}
