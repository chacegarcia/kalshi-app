using System.Text.Json;
using System.Text.Json.Serialization;
using KalshiBotWrapper.Data;

namespace KalshiBotWrapper.Dashboard;

// ── Runtime-configurable bot controls ────────────────────────────────────────

/// <summary>
/// Immutable record swapped atomically via a volatile reference.
/// All fields have safe defaults. Changes take effect on the next scan cycle.
/// </summary>
public sealed record BotControls
{
    [JsonPropertyName("executeEnabled")]
    public bool ExecuteEnabled { get; init; } = false;

    [JsonPropertyName("scanIntervalSeconds")]
    public int ScanIntervalSeconds { get; init; } = 120;

    [JsonPropertyName("maxBetsPerHour")]
    public int MaxBetsPerHour { get; init; } = 3;

    /// <summary>Only consider markets closing within this many hours from now.</summary>
    [JsonPropertyName("maxHoursToClose")]
    public double MaxHoursToClose { get; init; } = 24.0;

    /// <summary>
    /// Maximum cents below 50¢ that the mid price may be. Only YES-cheap markets qualify
    /// (mid &lt; 50). Default 15 → qualifies markets priced 35–49¢.
    /// </summary>
    [JsonPropertyName("nearFiftyMarginCents")]
    public int NearFiftyMarginCents { get; init; } = 15;

    /// <summary>
    /// Minimum payout margin in cents: the YES ask must be at least this many cents below 50¢
    /// before the bot considers a market. 0 = no minimum (any price below 50¢ qualifies).
    /// Example: 3 → only buy YES when ask ≤ 47¢ (at least a 3¢ payout edge over fair value).
    /// </summary>
    [JsonPropertyName("minPayoutMarginCents")]
    public int MinPayoutMarginCents { get; init; } = 2;

    /// <summary>
    /// Dollar amount to spend per bet in cents (e.g. 500 = $5.00).
    /// The number of contracts is calculated as floor(SpendPerBetCents / askPriceCents),
    /// clamped to a minimum of 1.
    /// </summary>
    [JsonPropertyName("spendPerBetCents")]
    public int SpendPerBetCents { get; init; } = 500;

    /// <summary>
    /// Maximum number of concurrent open positions before the bot stops placing new bets.
    /// Checked at the start of each execute pass. 0 = no limit.
    /// </summary>
    [JsonPropertyName("maxOpenPositions")]
    public int MaxOpenPositions { get; init; } = 10;

    /// <summary>
    /// If &gt; 0: automatically sell a long YES position during each scan pass when the best YES bid
    /// has fallen this many percent or more below the estimated entry price
    /// (e.g. 20 = sell when bid ≤ entry − 20% of entry). 0 = disabled.
    /// </summary>
    [JsonPropertyName("stopLossDownPct")]
    public double StopLossDownPct { get; init; } = 0.0;
}

// ── Suggestion record ─────────────────────────────────────────────────────────

/// <summary>
/// Records a market opportunity the scanner recommended, plus execution/outcome state.
/// </summary>
public sealed class SuggestionRecord
{
    public Guid Id { get; init; } = Guid.NewGuid();
    public string Ticker { get; init; } = "";
    public string EventTicker { get; init; } = "";
    public string Title { get; init; } = "";
    public int YesAskCents { get; init; }
    public int MidCents { get; init; }
    public int ContractCount { get; init; }
    public int SpendCents { get; init; }
    public DateTimeOffset SuggestedAt { get; init; }
    public DateTimeOffset CloseTime { get; init; }
    public int ScanRank { get; init; }          // 1 = highest payout pick in scan cycle
    public string Url { get; init; } = "";
    public bool Executed { get; set; }
    public DateTimeOffset? ExecutedAt { get; set; }
    public string? ExecuteError { get; set; }   // non-null when execution threw an exception
    public string? Resolution { get; set; }     // "yes" | "no" | null = unresolved
    /// <summary>
    /// Projected outcome in cents: positive = profit (YES won), negative = loss (YES lost).
    /// (100 − ask) × count when YES wins; −ask × count when YES loses.
    /// </summary>
    public int? OutcomeCents { get; set; }
}

// ── Opportunity row ───────────────────────────────────────────────────────────

public sealed record OpportunityRow(
    string Ticker,
    string EventTicker,
    string Title,
    DateTimeOffset CloseTime,
    int YesBidCents,
    int YesAskCents,
    int MidCents,
    double HoursToClose,
    string Url = ""
);

// ── Store ─────────────────────────────────────────────────────────────────────

/// <summary>
/// Thread-safe in-memory store for events, series, opportunities and runtime controls.
/// Replaces Python Flask monitor.py deques.
/// </summary>
public sealed class DashboardStore(BotRepository? repo = null)
{
    private const int MaxEvents      = 500;
    private const int MaxSeries      = 2000;
    private const int MaxSuggestions = 500;

    private readonly object _lock = new();
    private readonly LinkedList<Dictionary<string, object?>> _events = new();
    private readonly LinkedList<Dictionary<string, object?>> _series = new();
    private readonly List<SuggestionRecord> _suggestions = [];

    // Volatile reference — immutable record, reference writes are atomic on 64-bit
    private volatile BotControls _controls = new();
    private List<OpportunityRow> _opportunities = [];
    private Dictionary<string, (DateTimeOffset CloseTime, string Title, string Url)> _marketInfo = [];

    // ── Portfolio snapshot ────────────────────────────────────────────────────

    private int? _latestBalanceCents;
    private int  _latestSpentCents;

    public (int? BalanceCents, int SpentCents) GetPortfolioSummary()
    {
        lock (_lock) return (_latestBalanceCents, _latestSpentCents);
    }

    // ── Scan timing ───────────────────────────────────────────────────────────

    /// <summary>Released (max once) to make the polling loop skip its current delay and scan immediately.</summary>
    private readonly SemaphoreSlim _forceScanSignal = new(0, 1);

    private DateTimeOffset _lastScanAt = DateTimeOffset.MinValue;
    private DateTimeOffset _nextScanAt = DateTimeOffset.MinValue;
    private int    _lastScanTotal   = -1; // -1 = no scan yet
    private int    _lastScanMatched = -1;
    private string _lastSkipReason  = "";  // why no bet was placed in the last scan

    public SemaphoreSlim ForceScanSignal => _forceScanSignal;

    public void SetLastScanAt(DateTimeOffset t) { lock (_lock) _lastScanAt = t; }
    public void SetNextScanAt(DateTimeOffset t) { lock (_lock) _nextScanAt = t; }

    public void SetLastScanStats(int totalFetched, int matched, string skipReason = "")
    {
        lock (_lock) { _lastScanTotal = totalFetched; _lastScanMatched = matched; _lastSkipReason = skipReason; }
    }

    public (DateTimeOffset LastScan, DateTimeOffset NextScan, int Total, int Matched, string SkipReason) GetScanTiming()
    {
        lock (_lock) return (_lastScanAt, _nextScanAt, _lastScanTotal, _lastScanMatched, _lastSkipReason);
    }

    /// <summary>Signals the bot loop to scan immediately. No-op if a signal is already pending.</summary>
    public bool TriggerForceScan()
    {
        if (_forceScanSignal.CurrentCount == 0)
        {
            _forceScanSignal.Release();
            return true;
        }
        return false; // already pending
    }

    // ── Controls ──────────────────────────────────────────────────────────────

    public BotControls GetControls() => _controls;

    public void SetControls(BotControls controls)
    {
        _controls = controls;
        repo?.SaveControlsBackground(controls);
    }

    // ── Opportunities ─────────────────────────────────────────────────────────

    public void SetOpportunities(List<OpportunityRow> rows)
    {
        lock (_lock) _opportunities = rows;
    }

    public IReadOnlyList<OpportunityRow> GetOpportunities()
    {
        lock (_lock) return _opportunities.ToList();
    }

    /// <summary>Caches close time, title, and canonical URL for ALL fetched markets.</summary>
    public void SetMarketInfo(Dictionary<string, (DateTimeOffset CloseTime, string Title, string Url)> info)
    {
        lock (_lock) _marketInfo = info;
    }

    public (DateTimeOffset? CloseTime, string? Title, string? Url) GetMarketInfo(string ticker)
    {
        lock (_lock) return _marketInfo.TryGetValue(ticker, out var v) ? (v.CloseTime, v.Title, v.Url) : (null, null, null);
    }

    // ── Suggestions ───────────────────────────────────────────────────────────

    /// <summary>
    /// Records the top opportunities from a scan as suggestions.
    /// Skips tickers that already have a pending (unresolved) suggestion to avoid
    /// flooding the history when the same market tops multiple scan cycles.
    /// </summary>
    public void RecordSuggestions(IReadOnlyList<OpportunityRow> opps, BotControls controls)
    {
        if (opps.Count == 0) return;
        var now = DateTimeOffset.UtcNow;
        lock (_lock)
        {
            var pendingTickers = _suggestions
                .Where(s => s.Resolution == null)
                .Select(s => s.Ticker)
                .ToHashSet();

            int rank = 1;
            foreach (var opp in opps.Take(5))
            {
                if (!pendingTickers.Contains(opp.Ticker))
                {
                    var count = opp.YesAskCents > 0
                        ? Math.Max(1, controls.SpendPerBetCents / opp.YesAskCents)
                        : 1;
                    var record = new SuggestionRecord
                    {
                        Ticker        = opp.Ticker,
                        EventTicker   = opp.EventTicker,
                        Title         = opp.Title,
                        YesAskCents   = opp.YesAskCents,
                        MidCents      = opp.MidCents,
                        ContractCount = count,
                        SpendCents    = count * opp.YesAskCents,
                        SuggestedAt   = now,
                        CloseTime     = opp.CloseTime,
                        ScanRank      = rank,
                        Url           = opp.Url,
                    };
                    _suggestions.Add(record);
                    pendingTickers.Add(opp.Ticker);
                    repo?.InsertSuggestionBackground(record);
                }
                rank++;
            }
            while (_suggestions.Count > MaxSuggestions) _suggestions.RemoveAt(0);
        }
    }

    /// <summary>Marks the most recent pending suggestion for a ticker as executed.</summary>
    public void MarkSuggestionExecuted(string ticker)
    {
        var now = DateTimeOffset.UtcNow;
        SuggestionRecord? matched;
        lock (_lock)
        {
            matched = _suggestions.LastOrDefault(s => s.Ticker == ticker && !s.Executed);
            if (matched is not null) { matched.Executed = true; matched.ExecutedAt = now; }
        }
        if (matched is not null)
            repo?.MarkExecutedBackground(matched.Id, now);
    }

    /// <summary>Marks the most recent pending suggestion for a ticker with an execution error.</summary>
    public void MarkSuggestionError(string ticker, string error)
    {
        SuggestionRecord? matched;
        lock (_lock)
        {
            matched = _suggestions.LastOrDefault(s => s.Ticker == ticker && !s.Executed);
            if (matched is not null) matched.ExecuteError = error;
        }
        if (matched is not null)
            repo?.MarkErrorBackground(matched.Id, error);
    }

    /// <summary>Records the outcome of a suggestion once the market resolves.</summary>
    public void ResolveSuggestion(string ticker, string resolution)
    {
        int? outcomeCents = null;
        lock (_lock)
        {
            foreach (var s in _suggestions.Where(s => s.Ticker == ticker && s.Resolution == null))
            {
                s.Resolution   = resolution;
                s.OutcomeCents = resolution == "yes"
                    ? (100 - s.YesAskCents) * s.ContractCount
                    : -s.YesAskCents        * s.ContractCount;
                outcomeCents = s.OutcomeCents;
            }
        }
        repo?.ResolveBackground(ticker, resolution, outcomeCents);
    }

    public IReadOnlyList<SuggestionRecord> GetSuggestions()
    {
        lock (_lock) return _suggestions.ToList();
    }

    // ── Events ────────────────────────────────────────────────────────────────

    public void RecordEvent(string kind, object? payload = null)
    {
        var row = new Dictionary<string, object?>
        {
            ["kind"]   = kind,
            ["ts_iso"] = DateTimeOffset.UtcNow.ToString("o"),
            ["unix"]   = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
        };

        if (payload is not null)
        {
            var json = JsonSerializer.Serialize(payload);
            using var doc = JsonDocument.Parse(json);
            foreach (var prop in doc.RootElement.EnumerateObject())
                row[prop.Name] = prop.Value.Clone();
        }

        lock (_lock)
        {
            _events.AddFirst(row);
            while (_events.Count > MaxEvents) _events.RemoveLast();
        }

        repo?.WriteEventBackground(kind, payload);
    }

    // ── Series ────────────────────────────────────────────────────────────────

    public void RecordPortfolioSeriesPoint(
        int? balanceCents, int contractCount, int betsPlaced, int spentCents)
    {
        var row = new Dictionary<string, object?>
        {
            ["unix"]            = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
            ["ts_iso"]          = DateTimeOffset.UtcNow.ToString("o"),
            ["balance_cents"]   = balanceCents.HasValue ? Math.Max(0, balanceCents.Value) : 0,
            ["contract_count"]  = contractCount,
            ["bets_placed"]     = betsPlaced,
            ["spent_cents"]     = spentCents,
        };

        lock (_lock)
        {
            _latestBalanceCents = balanceCents;
            _latestSpentCents   = spentCents;
            _series.AddLast(row);
            while (_series.Count > MaxSeries) _series.RemoveFirst();
        }

        repo?.WriteSeriesPointBackground(balanceCents, contractCount, betsPlaced, spentCents);
    }

    public void Heartbeat(string note = "running")
        => RecordEvent("heartbeat", new { note });

    public IReadOnlyList<Dictionary<string, object?>> GetEvents()
    {
        lock (_lock) return _events.ToList();
    }

    public IReadOnlyList<Dictionary<string, object?>> GetSeries()
    {
        lock (_lock) return _series.ToList();
    }
}
