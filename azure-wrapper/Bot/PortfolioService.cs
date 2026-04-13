using KalshiBotWrapper.Kalshi;

namespace KalshiBotWrapper.Bot;

/// <summary>
/// Portfolio snapshot and position helpers. Mirrors Python portfolio.py.
/// </summary>
public sealed record PortfolioSnapshot(
    Dictionary<string, double> PositionsByTicker,
    Dictionary<string, int> RestingOrdersByTicker,
    int? BalanceCents,
    double TotalExposureCents);

public static class PortfolioService
{
    public static async Task<int?> GetBalanceCentsAsync(KalshiRestClient client, CancellationToken ct = default)
    {
        var resp = await client.GetBalanceAsync(ct);
        return resp.Balance;
    }

    public static async Task<PortfolioSnapshot> FetchPortfolioSnapshotAsync(
        KalshiRestClient client,
        string? ticker = null,
        CancellationToken ct = default)
    {
        var balResp = await client.GetBalanceAsync(ct);
        var balanceCents = balResp.Balance;

        // Positions for the specific ticker
        var posResp = await client.GetPositionsAsync(ticker, "position", 500, ct);
        var positions = PositionContractsByTicker(posResp.MarketPositions);

        // All positions for total exposure
        var allPosResp = await client.GetPositionsAsync(null, "position", 1000, ct);
        var exposure = TotalExposureCents(allPosResp.MarketPositions);

        // Resting orders
        var restingBy = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        string? cursor = null;
        do
        {
            var ords = await client.GetOrdersAsync("resting", ticker, 200, cursor, ct);
            foreach (var o in ords.Orders)
            {
                if (o.Ticker is null) continue;
                restingBy.TryGetValue(o.Ticker, out var cnt);
                restingBy[o.Ticker] = cnt + 1;
            }
            cursor = ords.Cursor;
        } while (!string.IsNullOrEmpty(cursor));

        return new PortfolioSnapshot(positions, restingBy, balanceCents, exposure);
    }

    public static async Task<MarketPosition?> GetMarketPositionRowAsync(
        KalshiRestClient client, string ticker, CancellationToken ct = default)
    {
        var resp = await client.GetPositionsAsync(ticker, "position", 50, ct);
        return resp.MarketPositions.FirstOrDefault(p =>
            string.Equals(p.Ticker, ticker, StringComparison.OrdinalIgnoreCase));
    }

    /// <summary>Rough average YES entry in cents from total_traded / position.</summary>
    public static int? EstimateYesEntryCentsFromPosition(MarketPosition p)
    {
        if (p.Position is null || p.TotalTraded is null) return null;
        var contracts = Math.Abs((double)p.Position.Value);
        if (contracts < 1e-9) return null;
        var perContract = p.TotalTraded.Value / contracts;
        var cents = (int)Math.Round(perContract * 100.0);
        return Math.Max(1, Math.Min(99, cents));
    }

    private static Dictionary<string, double> PositionContractsByTicker(List<MarketPosition> positions)
    {
        var out_ = new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase);
        foreach (var p in positions)
        {
            if (p.Ticker is null || p.Position is null) continue;
            out_[p.Ticker] = (double)p.Position.Value;
        }
        return out_;
    }

    private static double TotalExposureCents(List<MarketPosition> positions)
    {
        var total = 0.0;
        foreach (var p in positions)
        {
            if (p.MarketExposure is null) continue;
            total += p.MarketExposure.Value * 100.0;
        }
        return total;
    }
}
