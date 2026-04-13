using System.Text.Json;

namespace KalshiBotWrapper.Bot;

/// <summary>
/// Price record for backtesting. Mirrors Python backtest.PriceRecord.
/// </summary>
public sealed record PriceRecord(double Ts, string Ticker, double YesBidDollars, double YesAskDollars);

/// <summary>
/// Backtesting engine. Mirrors Python backtest.py.
/// </summary>
public static class BacktestEngine
{
    public static List<PriceRecord> LoadPriceRecordsJsonl(string path)
    {
        var rows = new List<PriceRecord>();
        foreach (var line in File.ReadAllLines(path))
        {
            var trimmed = line.Trim();
            if (string.IsNullOrEmpty(trimmed)) continue;
            using var doc = JsonDocument.Parse(trimmed);
            var r = doc.RootElement;
            rows.Add(new PriceRecord(
                r.GetProperty("ts").GetDouble(),
                r.GetProperty("ticker").GetString() ?? "",
                r.GetProperty("yes_bid_dollars").GetDouble(),
                r.GetProperty("yes_ask_dollars").GetDouble()));
        }
        rows.Sort((a, b) => a.Ts.CompareTo(b.Ts));
        return rows;
    }

    public static (List<TradeOutcome> Trades, List<double> Equity, PaperPortfolio Portfolio)
        RunRuleBacktest(
            IReadOnlyList<PriceRecord> records,
            Func<PriceRecord, TradeIntent?> strategyFn,
            PaperFillConfig cfg,
            double initialCashCents = 100_000.0)
    {
        var port = new PaperPortfolio(initialCashCents);
        var trades = new List<TradeOutcome>();

        foreach (var rec in records)
        {
            var midCents = (rec.YesBidDollars + rec.YesAskDollars) / 2.0 * 100.0;
            var intent = strategyFn(rec);
            if (intent is null)
            {
                port.MarkEquity(midCents);
                continue;
            }
            var (outcome, fee) = PaperSimulator.SimulateFill(intent, rec.YesBidDollars, rec.YesAskDollars, cfg);
            if (outcome is not null)
            {
                var filled = intent.Count * cfg.PartialFillFraction;
                port.ApplyBuyYes(filled, intent.YesPriceCents, fee);
                trades.Add(outcome);
            }
            port.MarkEquity(midCents);
        }

        return (trades, port.EquityHistory, port);
    }

    public static List<Dictionary<string, object>> ParameterSweep(
        IReadOnlyList<PriceRecord> records,
        Dictionary<string, List<object>> grid,
        Func<Dictionary<string, object>, Func<PriceRecord, TradeIntent?>> strategyFactory,
        PaperFillConfig cfg)
    {
        var keys = grid.Keys.ToList();
        var valueLists = keys.Select(k => grid[k]).ToList();
        var results = new List<Dictionary<string, object>>();

        foreach (var combo in CartesianProduct(valueLists))
        {
            var param = keys.Zip(combo).ToDictionary(x => x.First, x => x.Second);
            var fn = strategyFactory(param);
            var (tr, eq, _) = RunRuleBacktest(records, fn, cfg);
            results.Add(new Dictionary<string, object>
            {
                ["params"] = param,
                ["n_trades"] = tr.Count,
                ["max_dd"] = Metrics.MaxDrawdown(eq),
                ["report"] = Metrics.FormatReport(tr, eq),
            });
        }
        return results;
    }

    private static IEnumerable<List<object>> CartesianProduct(List<List<object>> lists)
    {
        IEnumerable<List<object>> seed = new[] { new List<object>() };
        return lists.Aggregate(seed, (acc, next) =>
            acc.SelectMany(a => next.Select(n =>
            {
                var combined = new List<object>(a) { n };
                return combined;
            })));
    }
}
