using System.Text.Json;
using KalshiBotWrapper.Bot;
using KalshiBotWrapper.Dashboard;
using KalshiBotWrapper.Kalshi;

namespace KalshiBotWrapper.Services;

/// <summary>
/// Runs the Kalshi trading bot entirely in-process (no Python subprocess).
/// Command is read from Bot__Command env var / appsettings.
/// </summary>
public sealed class BotRunnerService : BackgroundService
{
    private readonly ILogger<BotRunnerService> _log;
    private readonly IConfiguration _config;
    private readonly DashboardStore _store;
    private bool _firstScan = true;

    // Cumulative lifetime counters (reset on container restart)
    private int _totalBetsPlaced    = 0;
    private int _totalSpentCents    = 0;
    private int _currentContractCount = 0; // updated each scan from live positions

    public BotRunnerService(ILogger<BotRunnerService> log, IConfiguration config, DashboardStore store)
    {
        _log = log;
        _config = config;
        _store = store;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await Task.Delay(TimeSpan.FromSeconds(1), stoppingToken);

        var settings = TradingSettings.FromEnvironment();
        var command = (_config["Bot:Command"]
            ?? Environment.GetEnvironmentVariable("Bot__Command") ?? "run").Trim();
        var extraArgs = (_config["Bot:ExtraArgs"]
            ?? Environment.GetEnvironmentVariable("Bot__ExtraArgs") ?? "").Trim();
        var args = extraArgs.Split(' ', StringSplitOptions.RemoveEmptyEntries);

        LogStartupDiagnostics(settings, command);

        try
        {
            await DispatchAsync(command, args, settings, stoppingToken);
        }
        catch (OperationCanceledException)
        {
            _log.LogInformation("[bot] stopped by shutdown signal");
        }
        catch (KalshiApiException ex) when (ex.StatusCode == 401)
        {
            var keyId = settings.KalshiApiKeyId;
            var preview = string.IsNullOrWhiteSpace(keyId) ? "(NOT SET)"
                : keyId.Length > 8 ? keyId[..4] + "…" + keyId[^4..] : keyId;

            _log.LogError(
                "[bot] 401 Unauthorized — Kalshi rejected the credentials.\n" +
                "  KALSHI_API_KEY_ID : {KeyId}\n" +
                "  KALSHI_ENV        : {Env}\n" +
                "  REST URL          : {Url}\n\n" +
                "CHECKLIST:\n" +
                "  1. When you regenerated the private key a NEW Key ID was also issued — update KALSHI_API_KEY_ID.\n" +
                "  2. KALSHI_ENV must match where the key was created (demo vs prod).\n" +
                "  3. The PEM must be the PRIVATE key (-----BEGIN PRIVATE KEY-----).",
                preview, settings.KalshiEnv, settings.RestBaseUrl);

            _store.RecordEvent("auth_401", new
            {
                message = "401 Unauthorized — see container logs for checklist",
                key_id_preview = preview,
                kalshi_env = settings.KalshiEnv,
            });
            await Task.Delay(Timeout.Infinite, stoppingToken);
        }
        catch (Exception ex)
        {
            _log.LogError(ex, "[bot] CRASHED — hanging so container stays alive for log inspection");
            _store.RecordEvent("error", new { kind = "crash", message = ex.Message });
            await Task.Delay(Timeout.Infinite, stoppingToken);
        }
    }

    // ── Dispatch ──────────────────────────────────────────────────────────────

    private async Task DispatchAsync(
        string command, string[] args, TradingSettings settings, CancellationToken ct)
    {
        switch (command.ToLowerInvariant().Replace('_', '-'))
        {
            case "run":
                await CmdRunBotAsync(settings, ct);
                break;

            case "scan":
                await CmdScanAsync(settings, ct);
                break;

            case "list-markets":
                await CmdListMarketsAsync(settings, ct);
                break;

            case "cancel-all":
                await CmdCancelAllAsync(settings, ct);
                break;

            default:
                _log.LogWarning("[bot] Unknown command '{Command}' — defaulting to run", command);
                await CmdRunBotAsync(settings, ct);
                break;
        }
    }

    // ── Main polling loop ─────────────────────────────────────────────────────

    private async Task CmdRunBotAsync(TradingSettings settings, CancellationToken ct)
    {
        var auth = BuildAuthWithLogging(settings);
        using var client = new KalshiRestClient(settings.RestBaseUrl, auth);
        var risk = new RiskManager(settings);
        var ledger = new DryRunLedger();

        // Sliding 1-hour window tracking bet timestamps
        var betTimestamps = new Queue<DateTimeOffset>();

        _log.LogInformation("[bot] Opportunity scanner started");
        _store.Heartbeat("opportunity scanner started");

        while (!ct.IsCancellationRequested)
        {
            var controls = _store.GetControls();

            try
            {
                _store.SetLastScanAt(DateTimeOffset.UtcNow);
                await ScanAndExecuteAsync(client, settings, risk, ledger, controls, betTimestamps, ct);
            }
            catch (OperationCanceledException) { return; }
            catch (KalshiApiException ex) when (ex.StatusCode == 401) { throw; }
            catch (Exception ex)
            {
                _log.LogError(ex, "[scan] failed");
                _store.RecordEvent("scan_error", new { message = ex.Message });
            }

            var interval = TimeSpan.FromSeconds(Math.Max(10, controls.ScanIntervalSeconds));
            var nextAt = DateTimeOffset.UtcNow + interval;
            _store.SetNextScanAt(nextAt);
            _log.LogInformation("[scan] next scan at {NextAt} (in {Seconds}s)", nextAt.ToString("HH:mm:ss"), interval.TotalSeconds);

            // Wait for the interval — but wake up immediately if a force-scan is signalled
            try
            {
                await _store.ForceScanSignal.WaitAsync(interval, ct);
                _log.LogInformation("[scan] force scan triggered — skipping delay");
                _store.RecordEvent("force_scan", new { triggered_at = DateTimeOffset.UtcNow.ToString("o") });
            }
            catch (OperationCanceledException) { return; }
        }
    }

    private async Task ScanAndExecuteAsync(
        KalshiRestClient client,
        TradingSettings settings,
        RiskManager risk,
        DryRunLedger ledger,
        BotControls controls,
        Queue<DateTimeOffset> betTimestamps,
        CancellationToken ct)
    {
        // ── 1. Fetch ALL open markets (paginated) ─────────────────────────────
        // On first scan, log a raw sample so we can verify field names match the API response
        if (_firstScan)
        {
            _firstScan = false;
            try
            {
                var raw = await client.GetRawFirstMarketJsonAsync(ct);
                _log.LogInformation("[scan-debug] Raw API sample (1 market): {Raw}", raw);
            }
            catch (Exception ex)
            {
                _log.LogWarning(ex, "[scan-debug] Could not fetch raw sample");
            }
        }

        var allMarkets = await client.GetAllMarketsAsync("open", "exclude", ct);
        var now = DateTimeOffset.UtcNow;

        _log.LogInformation("[scan] Fetched {Total} open markets from Kalshi", allMarkets.Count);

        // ── 2. Prune expired bet timestamps (sliding 1-hour window) ───────────
        var windowStart = now.AddHours(-1);
        while (betTimestamps.Count > 0 && betTimestamps.Peek() < windowStart)
            betTimestamps.Dequeue();

        // ── 3. Filter for opportunities ───────────────────────────────────────
        var opportunities   = new List<OpportunityRow>();
        int droppedNoPrice  = 0;
        int droppedTime     = 0;
        int droppedProb     = 0;

        foreach (var m in allMarkets)
        {
            // Price resolution (best to worst):
            //  1. YES bid + YES ask from the YES book
            //  2. Implied YES prices derived from the NO book (100 - no_ask, 100 - no_bid)
            //  3. Last traded price
            int mid, bid, ask;
            if (m.YesBid is not null && m.YesAsk is not null)
            {
                bid = m.YesBid.Value;
                ask = m.YesAsk.Value;
                mid = (bid + ask) / 2;
            }
            else if (m.YesBidFromNo is not null && m.YesAskFromNo is not null)
            {
                bid = m.YesBidFromNo.Value;
                ask = m.YesAskFromNo.Value;
                mid = (bid + ask) / 2;
            }
            else if (m.LastPrice is not null)
            {
                bid = ask = mid = m.LastPrice.Value;
            }
            else
            {
                droppedNoPrice++;
                continue; // truly no price info
            }

            if (m.CloseTime is null) { droppedNoPrice++; continue; }

            var hoursToClose = (m.CloseTime.Value - now).TotalHours;
            if (hoursToClose <= 0 || hoursToClose > controls.MaxHoursToClose)
            {
                droppedTime++;
                continue;
            }

            // Only consider markets where YES is priced below 50¢ (payout > 2:1).
            // NearFiftyMarginCents caps how far below 50¢ we go (too cheap = unlikely to win).
            // MinPayoutMarginCents ensures the ask has at least a minimum edge vs fair value.
            if (mid >= 50 || (50 - mid) > controls.NearFiftyMarginCents)
            {
                droppedProb++;
                continue;
            }
            if (controls.MinPayoutMarginCents > 0 && (50 - ask) < controls.MinPayoutMarginCents)
            {
                droppedProb++;
                continue;
            }

            opportunities.Add(new OpportunityRow(
                m.Ticker,
                m.EventTicker ?? m.Ticker,
                m.Title.Length > 120 ? m.Title[..120] : m.Title,
                m.CloseTime.Value,
                bid,
                ask,
                mid,
                hoursToClose,
                m.KalshiUrl));
        }

        // Sort: highest payout first (lowest ask price = most cents below 50¢)
        opportunities.Sort((a, b) => a.YesAskCents.CompareTo(b.YesAskCents));

        // ── 4. Publish to dashboard ───────────────────────────────────────────
        _store.SetMarketInfo(allMarkets
            .Where(m => m.CloseTime.HasValue)
            .ToDictionary(m => m.Ticker, m => (m.CloseTime!.Value, m.Title, m.KalshiUrl)));
        _store.SetOpportunities(opportunities);
        _store.RecordSuggestions(opportunities, controls);

        // ── 5. Execute if enabled and under hourly / position limits ─────────
        string skipReason = "";
        int betsThisScan  = 0;

        _store.SetLastScanStats(allMarkets.Count, opportunities.Count, skipReason);
        _store.RecordEvent("scan", new
        {
            total_markets    = allMarkets.Count,
            count            = opportunities.Count,
            dropped_no_price = droppedNoPrice,
            dropped_time     = droppedTime,
            dropped_prob     = droppedProb,
            execute_enabled  = controls.ExecuteEnabled,
            bets_this_hour   = betTimestamps.Count,
            max_bets_per_hour= controls.MaxBetsPerHour,
            probability_range= $"{50 - controls.NearFiftyMarginCents}–49¢ (below-50 only, margin={controls.NearFiftyMarginCents}¢, minPayout={controls.MinPayoutMarginCents}¢)",
            max_hours        = $"{controls.MaxHoursToClose}h",
            spend_per_bet    = $"${controls.SpendPerBetCents / 100.0:F2}",
        });

        _log.LogInformation(
            "[scan] {Total} fetched → {Count} qualify | dropped: no_price={NP} time={DT} prob={DP} | window=<{MaxH}h below50-margin={Margin}¢ minPayout={MinPayout}¢ | execute={Execute} | bets={Bets}/{Max}",
            allMarkets.Count, opportunities.Count,
            droppedNoPrice, droppedTime, droppedProb,
            controls.MaxHoursToClose, controls.NearFiftyMarginCents, controls.MinPayoutMarginCents,
            controls.ExecuteEnabled, betTimestamps.Count, controls.MaxBetsPerHour);

        if (opportunities.Count > 0)
        {
            foreach (var opp in opportunities.Take(5))
                _log.LogInformation(
                    "  → {Ticker} mid={Mid}¢ closes_in={Hours:F1}h bid={Bid}¢ ask={Ask}¢  {Title}",
                    opp.Ticker, opp.MidCents, opp.HoursToClose,
                    opp.YesBidCents, opp.YesAskCents,
                    opp.Title.Length > 60 ? opp.Title[..60] : opp.Title);
        }

        if (opportunities.Count == 0)
        {
            skipReason = "No opportunities matched the current scan criteria";
        }
        else if (!controls.ExecuteEnabled)
        {
            skipReason = "Auto-execute is disabled — review suggestions and execute manually";
            _log.LogInformation("[scan] execute_enabled=false — suggestions only");
        }
        else
        {
            foreach (var opp in opportunities)
            {
                if (betTimestamps.Count >= controls.MaxBetsPerHour)
                {
                    skipReason = $"Hourly bet limit reached ({controls.MaxBetsPerHour}/hr) — next slot opens after {betTimestamps.Peek().AddHours(1):HH:mm} UTC";
                    _log.LogInformation("[scan] hourly limit reached ({Max}/hr) — skipping remaining", controls.MaxBetsPerHour);
                    break;
                }

                if (controls.MaxOpenPositions > 0 && _currentContractCount >= controls.MaxOpenPositions)
                {
                    skipReason = $"Open position limit reached ({_currentContractCount}/{controls.MaxOpenPositions}) — close a position to allow new bets";
                    _log.LogInformation("[scan] open position limit reached ({Count}/{Max}) — skipping remaining",
                        _currentContractCount, controls.MaxOpenPositions);
                    break;
                }

                // Derive contract count from spend budget: floor(budget / ask), minimum 1
                var contractCount = opp.YesAskCents > 0
                    ? Math.Max(1, controls.SpendPerBetCents / opp.YesAskCents)
                    : 1;

                var intent = new TradeIntent(
                    Ticker: opp.Ticker,
                    Side: "yes",
                    Action: "buy",
                    Count: contractCount,
                    YesPriceCents: opp.YesAskCents,
                    TimeInForce: "good_till_canceled");

                _log.LogInformation(
                    "[execute] Placing order: {Ticker} buy YES @ {Price}¢ x{Count} (budget ${Budget:F2})",
                    intent.Ticker, intent.YesPriceCents, intent.Count,
                    controls.SpendPerBetCents / 100.0);

                try
                {
                    await OrderExecution.ExecuteIntentAsync(
                        client, settings, risk, _log, _store, intent, ledger, ct);
                    _store.MarkSuggestionExecuted(intent.Ticker);
                    betTimestamps.Enqueue(DateTimeOffset.UtcNow);
                    _totalBetsPlaced++;
                    _totalSpentCents += intent.YesPriceCents * contractCount;
                    betsThisScan++;
                }
                catch (OperationCanceledException) { throw; }
                catch (Exception ex)
                {
                    _log.LogError(ex, "[execute_error] ticker={Ticker}", intent.Ticker);
                    _store.MarkSuggestionError(intent.Ticker, ex.Message);
                }
            }
        }

        // ── 6. Resolve expired suggestions ───────────────────────────────────
        await TryResolveSuggestionsAsync(client, ct);

        // ── 7. Record portfolio snapshot for the chart ────────────────────────
        try
        {
            var balResp = await client.GetBalanceAsync(ct);
            var posResp = await client.GetPositionsAsync(countFilter: "position", limit: 1000, ct: ct);

            _currentContractCount = (int)Math.Round(posResp.MarketPositions
                .Where(p => p.Position.HasValue && Math.Abs(p.Position.Value) >= 0.5)
                .Sum(p => Math.Abs(p.Position!.Value)));

            _store.RecordPortfolioSeriesPoint(
                balResp.Balance, _currentContractCount, _totalBetsPlaced, _totalSpentCents);

            _log.LogInformation(
                "[portfolio] balance=${Bal:F2} contracts={Contracts} bets={Bets} spent=${Spent:F2} | position_limit={Cur}/{Max}",
                (balResp.Balance ?? 0) / 100.0, _currentContractCount,
                _totalBetsPlaced, _totalSpentCents / 100.0,
                _currentContractCount, controls.MaxOpenPositions > 0 ? controls.MaxOpenPositions.ToString() : "∞");
        }
        catch (Exception ex)
        {
            _log.LogWarning(ex, "[portfolio] Could not fetch portfolio snapshot");
        }
    }

    // ── Suggestion resolution ─────────────────────────────────────────────────

    /// <summary>
    /// For suggestions whose CloseTime has passed and are still unresolved,
    /// fetch the market from Kalshi to see if it has a result ("yes" / "no").
    /// Checks at most 8 markets per scan cycle to avoid API hammering.
    /// </summary>
    private async Task TryResolveSuggestionsAsync(KalshiRestClient client, CancellationToken ct)
    {
        var now = DateTimeOffset.UtcNow;
        var expired = _store.GetSuggestions()
            .Where(s => s.Resolution == null && s.CloseTime < now)
            .GroupBy(s => s.Ticker)          // one API call per ticker, not per record
            .Select(g => g.First())
            .Take(8)
            .ToList();

        foreach (var s in expired)
        {
            try
            {
                var resp = await client.GetMarketAsync(s.Ticker, ct);
                var result = resp.Market?.Result;
                if (!string.IsNullOrWhiteSpace(result))
                {
                    _store.ResolveSuggestion(s.Ticker, result);
                    _log.LogInformation("[suggest] Resolved {Ticker} → {Result}", s.Ticker, result);
                }
            }
            catch (Exception ex)
            {
                _log.LogDebug(ex, "[suggest] Could not resolve {Ticker}", s.Ticker);
            }
        }
    }

    // ── Other commands ────────────────────────────────────────────────────────

    private async Task CmdScanAsync(TradingSettings settings, CancellationToken ct)
    {
        var auth = BuildAuthWithLogging(settings);
        using var client = new KalshiRestClient(settings.RestBaseUrl, auth);
        var rows = await MarketScanner.ScanKalshiOpportunitiesAsync(client, settings, 40, ct);
        var report = MarketScanner.FormatScanReport(rows);
        _log.LogInformation("[scan]\n{Report}", report);
        _store.RecordEvent("scan_complete", new { rows = rows.Count });
    }

    private async Task CmdListMarketsAsync(TradingSettings settings, CancellationToken ct)
    {
        var auth = BuildAuthWithLogging(settings);
        using var client = new KalshiRestClient(settings.RestBaseUrl, auth);
        var resp = await MarketData.ListOpenMarketsAsync(client, 30, "exclude", ct);
        foreach (var m in resp.Markets)
            _log.LogInformation("{Ticker}\t{Status}\t{Title}",
                m.Ticker, m.Status, m.Title.Length > 80 ? m.Title[..80] : m.Title);
    }

    private async Task CmdCancelAllAsync(TradingSettings settings, CancellationToken ct)
    {
        var auth = BuildAuthWithLogging(settings);
        using var client = new KalshiRestClient(settings.RestBaseUrl, auth);
        var n = await OrderExecution.CancelAllRestingOrdersAsync(client, _log, ct);
        _log.LogInformation("[cancel-all] Requested cancel for {N} orders", n);
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private KalshiAuth BuildAuthWithLogging(TradingSettings settings)
    {
        var keyId = settings.KalshiApiKeyId;
        var keyIdDisplay = string.IsNullOrWhiteSpace(keyId) ? "(NOT SET)"
            : keyId.Length > 8 ? keyId[..4] + "…" + keyId[^4..] : keyId;

        string pemStatus;
        if (!string.IsNullOrWhiteSpace(settings.KalshiPrivateKeyPath))
        {
            var exists = File.Exists(settings.KalshiPrivateKeyPath.Trim());
            pemStatus = exists
                ? $"FOUND at {settings.KalshiPrivateKeyPath}"
                : $"NOT FOUND at {settings.KalshiPrivateKeyPath} ← check path";
        }
        else if (!string.IsNullOrWhiteSpace(settings.KalshiPrivateKeyPem))
        {
            pemStatus = "loaded from KALSHI_PRIVATE_KEY_PEM env var (inline)";
        }
        else
        {
            pemStatus = "NOT SET — set KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_PEM";
        }

        _log.LogInformation(
            "[auth] Building Kalshi credentials\n" +
            "  Key ID (KALSHI_API_KEY_ID) : {KeyId}\n" +
            "  Environment (KALSHI_ENV)   : {Env}\n" +
            "  REST base URL              : {Url}\n" +
            "  Private key PEM            : {PemStatus}",
            keyIdDisplay, settings.KalshiEnv, settings.RestBaseUrl, pemStatus);

        return KalshiAuthLoader.Build(keyId, settings.KalshiPrivateKeyPath, settings.KalshiPrivateKeyPem);
    }

    private void LogStartupDiagnostics(TradingSettings settings, string command)
    {
        var keyId = settings.KalshiApiKeyId;
        var keyIdDisplay = string.IsNullOrWhiteSpace(keyId) ? "(NOT SET — check KALSHI_API_KEY_ID)"
            : keyId.Length > 8 ? keyId[..4] + "…" + keyId[^4..] : keyId;

        var pemSource = !string.IsNullOrWhiteSpace(settings.KalshiPrivateKeyPath)
            ? $"file: {settings.KalshiPrivateKeyPath}"
            : !string.IsNullOrWhiteSpace(settings.KalshiPrivateKeyPem)
                ? "env: KALSHI_PRIVATE_KEY_PEM (inline)"
                : "(NOT SET)";

        var hasMicrosoft = !string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("MICROSOFT_CLIENT_ID"));
        var hasGitHub    = !string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("GITHUB_CLIENT_ID"));
        var authProvider = (hasMicrosoft, hasGitHub) switch
        {
            (true,  true)  => "microsoft + github",
            (true,  false) => "microsoft",
            (false, true)  => "github",
            _              => "none (disabled)",
        };

        _log.LogInformation(
            "[bot_credentials]\n" +
            "  KALSHI_API_KEY_ID : {KeyId}\n" +
            "  KALSHI_ENV        : {Env}\n" +
            "  REST base URL     : {Url}\n" +
            "  PEM source        : {PemSource}\n" +
            "  command           : {Command}\n" +
            "  dry_run           : {DryRun}\n" +
            "  auth_provider     : {AuthProvider}",
            keyIdDisplay, settings.KalshiEnv, settings.RestBaseUrl,
            pemSource, command, settings.DryRun, authProvider);

        var controls = _store.GetControls();
        _store.RecordEvent("startup", new
        {
            key_id_preview = keyIdDisplay,
            kalshi_env = settings.KalshiEnv,
            rest_url = settings.RestBaseUrl,
            pem_source = pemSource,
            command,
            dry_run = settings.DryRun,
            auth_provider = authProvider,
            execute_enabled = controls.ExecuteEnabled,
            scan_interval_seconds = controls.ScanIntervalSeconds,
            max_bets_per_hour = controls.MaxBetsPerHour,
        });
    }
}
