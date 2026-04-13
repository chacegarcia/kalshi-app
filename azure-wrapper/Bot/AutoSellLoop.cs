using KalshiBotWrapper.Dashboard;
using KalshiBotWrapper.Kalshi;
using Microsoft.Extensions.Logging;

namespace KalshiBotWrapper.Bot;

/// <summary>
/// REST-driven auto-sell loop. Mirrors Python auto_sell.py.
/// </summary>
public static class AutoSellLoop
{
    public static async Task RunAsync(
        KalshiRestClient client,
        TradingSettings settings,
        RiskManager risk,
        DryRunLedger ledger,
        DashboardStore store,
        ILogger log,
        string ticker,
        int? cliMinYesBidCents,
        double pollSeconds,
        int maxCycles,
        bool stopAfterOneSell,
        CancellationToken ct)
    {
        if (settings.TradeExitOnlyProfitMargin && settings.TradeExitMinProfitCentsPerContract is null)
            throw new InvalidOperationException(
                "TRADE_EXIT_ONLY_PROFIT_MARGIN=true requires TRADE_EXIT_MIN_PROFIT_CENTS_PER_CONTRACT");

        int cycle = 0;
        bool soldOnce = false;

        while ((maxCycles == 0 || cycle < maxCycles) && !ct.IsCancellationRequested)
        {
            cycle++;
            var snap = await PortfolioService.FetchPortfolioSnapshotAsync(client, ticker, ct);
            var signed = snap.PositionsByTicker.GetValueOrDefault(ticker, 0.0);

            if (signed <= 0)
            {
                log.LogInformation("[auto_sell_skip] no_long_yes ticker={Ticker} signed={Signed}", ticker, signed);
                if (stopAfterOneSell && soldOnce) return;
                await Task.Delay(TimeSpan.FromSeconds(pollSeconds), ct);
                continue;
            }

            int? entryRef = await ResolveEntryReferenceAsync(client, settings, ticker, log, ct);

            var ob = await MarketData.GetOrderbookAsync(client, ticker, ct);
            var best = MarketData.BestYesBidCents(ob);
            if (best is null)
            {
                log.LogInformation("[auto_sell_skip] no_yes_bids ticker={Ticker}", ticker);
                await Task.Delay(TimeSpan.FromSeconds(pollSeconds), ct);
                continue;
            }

            var (fire, reason) = ShouldFireExit(best.Value, settings, cliMinYesBidCents, entryRef);
            if (!fire)
            {
                log.LogInformation(
                    "[auto_sell_wait] ticker={Ticker} best={Best} effectiveMin={Min} detail={Detail}",
                    ticker, best, settings.AutoSellEffectiveMinYesBidCents(cliMinYesBidCents), reason);
                await Task.Delay(TimeSpan.FromSeconds(pollSeconds), ct);
                continue;
            }

            var count = Math.Min((int)signed, settings.MaxContractsPerMarket);
            if (count < 1) { await Task.Delay(TimeSpan.FromSeconds(pollSeconds), ct); continue; }

            var limitCents = Math.Max(1, best.Value - settings.TradeExitSellAggressionCents);
            var tif = settings.TradeExitSellTimeInForce;
            var intent = new TradeIntent(ticker, "yes", "sell", count, limitCents, tif);

            log.LogInformation(
                "[auto_sell_fire] ticker={Ticker} count={Count} limitCents={Limit} best={Best} trigger={Trigger}",
                ticker, count, limitCents, best, reason);

            await OrderExecution.ExecuteIntentAsync(client, settings, risk, log, store, intent, ledger, ct);
            soldOnce = true;
            if (stopAfterOneSell) return;
            await Task.Delay(TimeSpan.FromSeconds(pollSeconds), ct);
        }
    }

    private static async Task<int?> ResolveEntryReferenceAsync(
        KalshiRestClient client, TradingSettings settings, string ticker, ILogger log, CancellationToken ct)
    {
        if (settings.TradeExitEntryReferenceYesCents.HasValue)
            return settings.TradeExitEntryReferenceYesCents;
        if (!settings.TradeExitEstimateEntryFromPortfolio) return null;

        var row = await PortfolioService.GetMarketPositionRowAsync(client, ticker, ct);
        if (row is null) return null;
        var est = PortfolioService.EstimateYesEntryCentsFromPosition(row);
        if (est.HasValue)
            log.LogInformation("[auto_sell_entry_estimate] ticker={Ticker} est={Est}", ticker, est);
        return est;
    }

    private static (bool Fire, string Reason) ShouldFireExit(
        int bestBidCents, TradingSettings settings, int? cliMin, int? entryRefCents)
    {
        var tMin = settings.AutoSellEffectiveMinYesBidCents(cliMin);
        var pctHit = tMin.HasValue && bestBidCents >= tMin.Value;

        bool profitHit = false;
        if (settings.TradeExitMinProfitCentsPerContract.HasValue && entryRefCents.HasValue)
        {
            var need = entryRefCents.Value + settings.TradeExitMinProfitCentsPerContract.Value;
            profitHit = bestBidCents >= need;
        }

        if (settings.TradeExitOnlyProfitMargin)
            return profitHit ? (true, "take_profit_profit_margin") : (false, "wait_profit_only_mode");

        if (pctHit && profitHit) return (true, "take_profit_implied_pct_and_margin");
        if (pctHit) return (true, "take_profit_implied_pct");
        if (profitHit) return (true, "take_profit_profit_margin");
        return (false, "wait");
    }
}
