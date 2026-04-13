using KalshiBotWrapper.Dashboard;
using KalshiBotWrapper.Kalshi;
using Microsoft.Extensions.Logging;

namespace KalshiBotWrapper.Bot;

public sealed record SimulatedOrder(string ClientOrderId, string Ticker, DateTimeOffset CreatedAt);

/// <summary>
/// In-memory standing for paper mode. Mirrors Python execution.DryRunLedger.
/// </summary>
public sealed class DryRunLedger
{
    private readonly List<SimulatedOrder> _orders = [];
    public IReadOnlyList<SimulatedOrder> Orders => _orders;

    public SimulatedOrder RecordIntent(TradeIntent intent)
    {
        var sim = new SimulatedOrder(Guid.NewGuid().ToString(), intent.Ticker, DateTimeOffset.UtcNow);
        _orders.Add(sim);
        return sim;
    }
}

/// <summary>
/// Order placement, cancellation, dry-run simulation. Mirrors Python execution.py.
/// </summary>
public static class OrderExecution
{
    public static async Task<int> CancelAllRestingOrdersAsync(
        KalshiRestClient client, ILogger log, CancellationToken ct = default)
    {
        var ids = new List<string>();
        string? cursor = null;
        do
        {
            var resp = await client.GetOrdersAsync("resting", null, 200, cursor, ct);
            ids.AddRange(resp.Orders.Select(o => o.OrderId).Where(id => id != null)!);
            cursor = resp.Cursor;
        } while (!string.IsNullOrEmpty(cursor));

        int cancelled = 0;
        for (int i = 0; i < ids.Count; i += 20)
        {
            var chunk = ids.Skip(i).Take(20).ToList();
            if (chunk.Count == 0) break;
            await client.BatchCancelOrdersAsync(chunk, ct);
            cancelled += chunk.Count;
            log.LogInformation("[batch_cancel] {Ids}", string.Join(",", chunk));
        }
        return cancelled;
    }

    public static async Task<int> CancelStaleOrdersAsync(
        KalshiRestClient client, TradingSettings settings, ILogger log, CancellationToken ct = default)
    {
        var cutoff = DateTimeOffset.UtcNow.AddSeconds(-settings.StaleOrderSeconds);
        var staleIds = new List<string>();
        string? cursor = null;
        do
        {
            var resp = await client.GetOrdersAsync("resting", null, 200, cursor, ct);
            foreach (var o in resp.Orders)
            {
                if (o.OrderId is null || o.CreatedTime is null) continue;
                if (o.CreatedTime.Value < cutoff) staleIds.Add(o.OrderId);
            }
            cursor = resp.Cursor;
        } while (!string.IsNullOrEmpty(cursor));

        int cancelled = 0;
        for (int i = 0; i < staleIds.Count; i += 20)
        {
            var chunk = staleIds.Skip(i).Take(20).ToList();
            if (chunk.Count == 0) break;
            await client.BatchCancelOrdersAsync(chunk, ct);
            cancelled += chunk.Count;
            log.LogInformation("[stale_cancel] {Ids}", string.Join(",", chunk));
        }
        return cancelled;
    }

    public static async Task<CreateOrderResponse> PlaceLimitOrderLiveAsync(
        KalshiRestClient client, TradeIntent intent, CancellationToken ct = default)
    {
        var req = new CreateOrderRequest
        {
            Ticker = intent.Ticker,
            ClientOrderId = Guid.NewGuid().ToString(),
            Side = intent.Side,
            Action = intent.Action,
            Count = intent.Count,
            YesPrice = intent.YesPriceCents,
            TimeInForce = intent.TimeInForce,
        };
        return await client.CreateOrderAsync(req, ct);
    }

    public static async Task ExecuteIntentAsync(
        KalshiRestClient client,
        TradingSettings settings,
        RiskManager risk,
        ILogger log,
        DashboardStore store,
        TradeIntent intent,
        DryRunLedger? ledger = null,
        CancellationToken ct = default)
    {
        var snap = await PortfolioService.FetchPortfolioSnapshotAsync(client, intent.Ticker, ct);
        risk.RecordBalanceSample(snap.BalanceCents);

        // Cap for notional
        var capped = Sizing.CapBuyYesCountForNotional(
            intent.Count, intent.YesPriceCents, settings.TradeMaxOrderNotionalUsd,
            intent.Side, intent.Action);
        if (capped != intent.Count) intent = intent.WithCount(capped);

        var maxC = Sizing.EffectiveMaxContracts(settings, snap.BalanceCents, intent.YesPriceCents);
        var maxExp = Sizing.EffectiveMaxExposureCents(settings, snap.BalanceCents);
        if (intent.Count > maxC) intent = intent.WithCount(maxC);
        if (intent.Count < 1)
        {
            log.LogInformation("[order_blocked] zero_contracts_after_balance_sizing ticker={Ticker}", intent.Ticker);
            store.RecordEvent("blocked", new { reason = "zero_contracts_after_balance_sizing", ticker = intent.Ticker });
            return;
        }

        var signed = snap.PositionsByTicker.GetValueOrDefault(intent.Ticker, 0.0);
        var projectedAbs = TradeIntentHelper.ProjectedAbsPositionAfter(signed, intent);
        var resting = snap.RestingOrdersByTicker.GetValueOrDefault(intent.Ticker, 0);
        var addExp = intent.Side == "yes" && intent.Action == "buy"
            ? (double)(intent.Count * intent.YesPriceCents) : 0.0;

        var (allowed, reason) = risk.CheckNewOrder(
            intent.Ticker, intent.Count, projectedAbs, resting,
            snap.TotalExposureCents, addExp,
            orderIncreasesExposure: intent.Action == "buy",
            maxContractsOverride: maxC,
            maxExposureCentsOverride: maxExp);

        if (!allowed)
        {
            log.LogInformation("[order_blocked] {Reason} ticker={Ticker}", reason, intent.Ticker);
            store.RecordEvent("blocked", new { reason, ticker = intent.Ticker });
            return;
        }

        if (settings.DryRun)
        {
            var ldg = ledger ?? new DryRunLedger();
            var sim = ldg.RecordIntent(intent);
            risk.RecordOrderSubmitted(intent.Count);
            log.LogInformation("[dry_run_order] simId={SimId} ticker={Ticker} count={Count} priceCents={Price}",
                sim.ClientOrderId, intent.Ticker, intent.Count, intent.YesPriceCents);
            store.RecordEvent("dry_run", new
            {
                simulated_client_order_id = sim.ClientOrderId,
                ticker = intent.Ticker,
                count = intent.Count,
                yes_price_cents = intent.YesPriceCents,
            });
            return;
        }

        if (!settings.CanSendRealOrders)
        {
            log.LogWarning("[order_refused] LIVE_TRADING_false_or_misconfigured ticker={Ticker}", intent.Ticker);
            store.RecordEvent("refused", new { reason = "LIVE_TRADING_false_or_misconfigured", ticker = intent.Ticker });
            return;
        }

        if (settings.KalshiEnv == "prod")
            log.LogWarning("*** LIVE order submission enabled (env=prod). Verify limits.");

        log.LogInformation("[live_order_submit] ticker={Ticker} count={Count} priceCents={Price}",
            intent.Ticker, intent.Count, intent.YesPriceCents);
        store.RecordEvent("live_submit", new { env = settings.KalshiEnv, ticker = intent.Ticker, count = intent.Count });

        var resp = await PlaceLimitOrderLiveAsync(client, intent, ct);
        risk.RecordOrderSubmitted(intent.Count);
        log.LogInformation("[live_order_ack] orderId={OrderId}", resp.Order?.OrderId);
        store.RecordEvent("live_ack", new { order_id = resp.Order?.OrderId, ticker = intent.Ticker });
    }
}
