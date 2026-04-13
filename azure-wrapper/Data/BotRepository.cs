using System.Text.Json;
using KalshiBotWrapper.Dashboard;
using Microsoft.EntityFrameworkCore;

namespace KalshiBotWrapper.Data;

/// <summary>
/// Singleton service that persists events and suggestions to Azure SQL.
/// All writes are fire-and-forget — a DB error is logged but never crashes the bot.
/// Only registered when <c>SQL_CONNECTION_STRING</c> is set.
/// </summary>
public sealed class BotRepository(IDbContextFactory<BotDbContext> factory, ILogger<BotRepository> log)
{
    private readonly string _kalshiEnv = Environment.GetEnvironmentVariable("KALSHI_ENV") ?? "demo";
    private readonly bool   _dryRun    = string.Equals(
        Environment.GetEnvironmentVariable("DRY_RUN"), "true", StringComparison.OrdinalIgnoreCase);

    // ── Portfolio series ──────────────────────────────────────────────────────

    /// <summary>Fire-and-forget insert of one portfolio series point.</summary>
    public void WriteSeriesPointBackground(int? balanceCents, int contractCount, int betsPlaced, int spentCents)
    {
        var env = _kalshiEnv;
        var dry = _dryRun;
        _ = Task.Run(async () =>
        {
            try
            {
                await using var ctx = await factory.CreateDbContextAsync();
                ctx.PortfolioSeries.Add(new PortfolioSeriesEntity
                {
                    RecordedAt    = DateTime.UtcNow,
                    BalanceCents  = balanceCents,
                    ContractCount = contractCount,
                    BetsPlaced    = betsPlaced,
                    SpentCents    = spentCents,
                    KalshiEnv     = env,
                    DryRun        = dry,
                });
                await ctx.SaveChangesAsync();
            }
            catch (Exception ex)
            {
                log.LogDebug(ex, "[BotRepository] portfolio_series insert failed");
            }
        });
    }

    /// <summary>Returns the most recent <paramref name="limit"/> series points, oldest-first.</summary>
    public async Task<List<object>> GetSeriesAsync(int limit = 2000, CancellationToken ct = default)
    {
        try
        {
            await using var ctx = await factory.CreateDbContextAsync(ct);
            var rows = await ctx.PortfolioSeries
                .OrderByDescending(p => p.RecordedAt)
                .Take(limit)
                .OrderBy(p => p.RecordedAt)
                .ToListAsync(ct);

            return rows.Select(p => (object)new
            {
                unix          = new DateTimeOffset(p.RecordedAt, TimeSpan.Zero).ToUnixTimeMilliseconds() / 1000.0,
                ts_iso        = new DateTimeOffset(p.RecordedAt, TimeSpan.Zero).ToString("o"),
                balance_cents = p.BalanceCents.HasValue ? Math.Max(0, p.BalanceCents.Value) : 0,
                contract_count = p.ContractCount,
                bets_placed   = p.BetsPlaced,
                spent_cents   = p.SpentCents,
            }).ToList();
        }
        catch (Exception ex)
        {
            log.LogWarning(ex, "[BotRepository] GetSeries failed");
            return [];
        }
    }

    // ── Suggestions ──────────────────────────────────────────────────────────

    /// <summary>Returns all suggestions ordered oldest-first, with cumulative P&amp;L computed.</summary>
    public async Task<List<object>> GetSuggestionsAsync(CancellationToken ct = default)
    {
        try
        {
            await using var ctx = await factory.CreateDbContextAsync(ct);
            var rows = await ctx.SuggestionHistory
                .OrderBy(s => s.SuggestedAt)
                .ToListAsync(ct);

            int cumulative = 0;
            return rows.Select(s =>
            {
                if (s.Resolution != null) cumulative += s.OutcomeCents ?? 0;
                return (object)new
                {
                    id              = s.SuggestionId,
                    ticker          = s.Ticker,
                    eventTicker     = s.EventTicker,
                    title           = s.Title,
                    yesAskCents     = s.YesAskCents,
                    midCents        = s.MidCents,
                    contractCount   = s.ContractCount,
                    spendCents      = s.SpendCents,
                    suggestedAt     = s.SuggestedAt.ToString("o"),
                    suggestedAtUnix = s.SuggestedAt.ToUnixTimeSeconds() * 1000.0 / 1000.0,
                    closeTime       = s.CloseTime.ToString("o"),
                    scanRank        = s.ScanRank,
                    executed        = s.Executed,
                    executedAt      = s.ExecutedAt?.ToString("o"),
                    resolution      = s.Resolution,
                    outcomeCents    = s.OutcomeCents,
                    cumulativeCents = cumulative,
                    url             = s.Url,
                    executeError    = s.ExecuteError,
                };
            }).ToList();
        }
        catch (Exception ex)
        {
            log.LogWarning(ex, "[BotRepository] GetSuggestions failed");
            return [];
        }
    }

    // ── Controls ──────────────────────────────────────────────────────────────

    /// <summary>Loads saved <see cref="BotControls"/> from the DB. Returns null if no row exists yet.</summary>
    public async Task<Dashboard.BotControls?> LoadControlsAsync(CancellationToken ct = default)
    {
        try
        {
            await using var ctx = await factory.CreateDbContextAsync(ct);
            var row = await ctx.BotControls.FindAsync([1], ct);
            if (row is null) return null;
            return new Dashboard.BotControls
            {
                ExecuteEnabled       = row.ExecuteEnabled,
                ScanIntervalSeconds  = row.ScanIntervalSeconds,
                MaxBetsPerHour       = row.MaxBetsPerHour,
                MaxHoursToClose      = row.MaxHoursToClose,
                NearFiftyMarginCents = row.NearFiftyMarginCents,
                MinPayoutMarginCents = row.MinPayoutMarginCents,
                SpendPerBetCents     = row.SpendPerBetCents,
                MaxOpenPositions     = row.MaxOpenPositions,
            };
        }
        catch (Exception ex)
        {
            log.LogWarning(ex, "[BotRepository] LoadControls failed — using defaults");
            return null;
        }
    }

    /// <summary>Fire-and-forget upsert of <see cref="BotControls"/> into <c>bot_controls</c>.</summary>
    public void SaveControlsBackground(Dashboard.BotControls controls)
    {
        _ = Task.Run(async () =>
        {
            try
            {
                await using var ctx = await factory.CreateDbContextAsync();
                var row = await ctx.BotControls.FindAsync(1);
                if (row is null)
                {
                    ctx.BotControls.Add(new BotControlsEntity
                    {
                        Id                   = 1,
                        ExecuteEnabled       = controls.ExecuteEnabled,
                        ScanIntervalSeconds  = controls.ScanIntervalSeconds,
                        MaxBetsPerHour       = controls.MaxBetsPerHour,
                        MaxHoursToClose      = controls.MaxHoursToClose,
                        NearFiftyMarginCents = controls.NearFiftyMarginCents,
                        MinPayoutMarginCents = controls.MinPayoutMarginCents,
                        SpendPerBetCents     = controls.SpendPerBetCents,
                        MaxOpenPositions     = controls.MaxOpenPositions,
                        UpdatedAt            = DateTime.UtcNow,
                    });
                }
                else
                {
                    row.ExecuteEnabled       = controls.ExecuteEnabled;
                    row.ScanIntervalSeconds  = controls.ScanIntervalSeconds;
                    row.MaxBetsPerHour       = controls.MaxBetsPerHour;
                    row.MaxHoursToClose      = controls.MaxHoursToClose;
                    row.NearFiftyMarginCents = controls.NearFiftyMarginCents;
                    row.MinPayoutMarginCents = controls.MinPayoutMarginCents;
                    row.SpendPerBetCents     = controls.SpendPerBetCents;
                    row.MaxOpenPositions     = controls.MaxOpenPositions;
                    row.UpdatedAt            = DateTime.UtcNow;
                }
                await ctx.SaveChangesAsync();
            }
            catch (Exception ex)
            {
                log.LogDebug(ex, "[BotRepository] SaveControls upsert failed");
            }
        });
    }

    // ── Schema init ───────────────────────────────────────────────────────────

    // Each statement is idempotent — safe to run on every startup against an existing DB.
    private static readonly string[] SchemaSql =
    [
        """
        IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'event_history')
        CREATE TABLE event_history (
            Id         BIGINT        IDENTITY(1,1) PRIMARY KEY,
            CreatedAt  DATETIME2     NOT NULL,
            Kind       NVARCHAR(100) NOT NULL,
            Payload    NVARCHAR(MAX),
            KalshiEnv  NVARCHAR(10)  NOT NULL DEFAULT '',
            DryRun     BIT           NOT NULL DEFAULT 1
        )
        """,
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_event_history_created_at') CREATE INDEX ix_event_history_created_at ON event_history (CreatedAt DESC)",

        """
        IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'suggestion_history')
        CREATE TABLE suggestion_history (
            Id                  BIGINT         IDENTITY(1,1) PRIMARY KEY,
            CreatedAt           DATETIME2      NOT NULL,
            SuggestionId        UNIQUEIDENTIFIER NOT NULL,
            Ticker              NVARCHAR(100)  NOT NULL DEFAULT '',
            EventTicker         NVARCHAR(100)  NOT NULL DEFAULT '',
            Title               NVARCHAR(500)  NOT NULL DEFAULT '',
            YesAskCents         INT            NOT NULL DEFAULT 0,
            MidCents            INT            NOT NULL DEFAULT 0,
            ContractCount       INT            NOT NULL DEFAULT 0,
            SpendCents          INT            NOT NULL DEFAULT 0,
            SuggestedAt         DATETIMEOFFSET NOT NULL,
            CloseTime           DATETIMEOFFSET NOT NULL,
            ScanRank            INT            NOT NULL DEFAULT 0,
            Url                 NVARCHAR(500)  NOT NULL DEFAULT '',
            Executed            BIT            NOT NULL DEFAULT 0,
            ExecutedAt          DATETIMEOFFSET,
            Resolution          NVARCHAR(10),
            OutcomeCents        INT,
            KalshiEnv           NVARCHAR(10)   NOT NULL DEFAULT '',
            DryRun              BIT            NOT NULL DEFAULT 1
        )
        """,
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_suggestion_history_suggestion_id') CREATE INDEX ix_suggestion_history_suggestion_id ON suggestion_history (SuggestionId)",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_suggestion_history_created_at')   CREATE INDEX ix_suggestion_history_created_at   ON suggestion_history (CreatedAt DESC)",
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_suggestion_history_ticker')       CREATE INDEX ix_suggestion_history_ticker       ON suggestion_history (Ticker)",
        "IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('suggestion_history') AND name = 'ExecuteError') ALTER TABLE suggestion_history ADD ExecuteError NVARCHAR(500) NULL",

        """
        IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'bot_controls')
        CREATE TABLE bot_controls (
            Id                   INT            NOT NULL PRIMARY KEY DEFAULT 1,
            ExecuteEnabled       BIT            NOT NULL DEFAULT 0,
            ScanIntervalSeconds  INT            NOT NULL DEFAULT 120,
            MaxBetsPerHour       INT            NOT NULL DEFAULT 3,
            MaxHoursToClose      FLOAT          NOT NULL DEFAULT 24.0,
            NearFiftyMarginCents INT            NOT NULL DEFAULT 15,
            MinPayoutMarginCents INT            NOT NULL DEFAULT 2,
            SpendPerBetCents     INT            NOT NULL DEFAULT 500,
            MaxOpenPositions     INT            NOT NULL DEFAULT 10,
            UpdatedAt            DATETIME2      NOT NULL
        )
        """,

        """
        IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'portfolio_series')
        CREATE TABLE portfolio_series (
            Id            BIGINT    IDENTITY(1,1) PRIMARY KEY,
            RecordedAt    DATETIME2 NOT NULL,
            BalanceCents  INT,
            ContractCount INT       NOT NULL DEFAULT 0,
            BetsPlaced    INT       NOT NULL DEFAULT 0,
            SpentCents    INT       NOT NULL DEFAULT 0,
            KalshiEnv     NVARCHAR(10) NOT NULL DEFAULT '',
            DryRun        BIT          NOT NULL DEFAULT 1
        )
        """,
        "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_portfolio_series_recorded_at') CREATE INDEX ix_portfolio_series_recorded_at ON portfolio_series (RecordedAt DESC)",
    ];

    /// <summary>Called once at startup. Creates any missing tables — safe to run against an existing DB.</summary>
    public async Task EnsureSchemaAsync(CancellationToken ct = default)
    {
        try
        {
            await using var ctx = await factory.CreateDbContextAsync(ct);
            foreach (var sql in SchemaSql)
                await ctx.Database.ExecuteSqlRawAsync(sql, ct);
            log.LogInformation("[BotRepository] Schema ready");
        }
        catch (Exception ex)
        {
            log.LogWarning(ex, "[BotRepository] EnsureSchema failed — SQL persistence disabled for this run");
        }
    }

    // ── Events ────────────────────────────────────────────────────────────────

    /// <summary>Fire-and-forget insert into <c>event_history</c>.</summary>
    public void WriteEventBackground(string kind, object? payload)
    {
        var payloadJson = payload is null ? null : JsonSerializer.Serialize(payload);
        var env = _kalshiEnv;
        var dry = _dryRun;
        _ = Task.Run(async () =>
        {
            try
            {
                await using var ctx = await factory.CreateDbContextAsync();
                ctx.EventHistory.Add(new EventHistoryEntity
                {
                    CreatedAt = DateTime.UtcNow,
                    Kind      = kind,
                    Payload   = payloadJson,
                    KalshiEnv = env,
                    DryRun    = dry,
                });
                await ctx.SaveChangesAsync();
            }
            catch (Exception ex)
            {
                log.LogDebug(ex, "[BotRepository] event_history insert failed");
            }
        });
    }

    // ── Suggestions ───────────────────────────────────────────────────────────

    /// <summary>Fire-and-forget insert of a new <see cref="SuggestionRecord"/> row.</summary>
    public void InsertSuggestionBackground(SuggestionRecord record)
    {
        var env = _kalshiEnv;
        var dry = _dryRun;
        _ = Task.Run(async () =>
        {
            try
            {
                await using var ctx = await factory.CreateDbContextAsync();
                ctx.SuggestionHistory.Add(new SuggestionHistoryEntity
                {
                    SuggestionId  = record.Id,
                    Ticker        = record.Ticker,
                    EventTicker   = record.EventTicker,
                    Title         = record.Title,
                    YesAskCents   = record.YesAskCents,
                    MidCents      = record.MidCents,
                    ContractCount = record.ContractCount,
                    SpendCents    = record.SpendCents,
                    SuggestedAt   = record.SuggestedAt,
                    CloseTime     = record.CloseTime,
                    ScanRank      = record.ScanRank,
                    Url           = record.Url,
                    KalshiEnv     = env,
                    DryRun        = dry,
                    CreatedAt     = DateTime.UtcNow,
                    ExecuteError  = null,
                });
                await ctx.SaveChangesAsync();
            }
            catch (Exception ex)
            {
                log.LogDebug(ex, "[BotRepository] suggestion_history insert failed (ticker={Ticker})", record.Ticker);
            }
        });
    }

    /// <summary>Fire-and-forget update: stores an execution error on a suggestion.</summary>
    public void MarkErrorBackground(Guid suggestionId, string error)
    {
        _ = Task.Run(async () =>
        {
            try
            {
                await using var ctx = await factory.CreateDbContextAsync();
                var truncated = error.Length > 500 ? error[..500] : error;
                await ctx.SuggestionHistory
                    .Where(s => s.SuggestionId == suggestionId)
                    .ExecuteUpdateAsync(s => s
                        .SetProperty(x => x.ExecuteError, truncated));
            }
            catch (Exception ex)
            {
                log.LogDebug(ex, "[BotRepository] MarkError update failed (id={Id})", suggestionId);
            }
        });
    }

    /// <summary>Fire-and-forget update: marks a suggestion as executed.</summary>
    public void MarkExecutedBackground(Guid suggestionId, DateTimeOffset executedAt)
    {
        _ = Task.Run(async () =>
        {
            try
            {
                await using var ctx = await factory.CreateDbContextAsync();
                await ctx.SuggestionHistory
                    .Where(s => s.SuggestionId == suggestionId)
                    .ExecuteUpdateAsync(s => s
                        .SetProperty(x => x.Executed,   true)
                        .SetProperty(x => x.ExecutedAt, executedAt));
            }
            catch (Exception ex)
            {
                log.LogDebug(ex, "[BotRepository] MarkExecuted update failed (id={Id})", suggestionId);
            }
        });
    }

    /// <summary>Fire-and-forget update: records the resolution outcome for all pending suggestions on a ticker.</summary>
    public void ResolveBackground(string ticker, string resolution, int? outcomeCents)
    {
        _ = Task.Run(async () =>
        {
            try
            {
                await using var ctx = await factory.CreateDbContextAsync();
                await ctx.SuggestionHistory
                    .Where(s => s.Ticker == ticker && s.Resolution == null)
                    .ExecuteUpdateAsync(s => s
                        .SetProperty(x => x.Resolution,   resolution)
                        .SetProperty(x => x.OutcomeCents, outcomeCents));
            }
            catch (Exception ex)
            {
                log.LogDebug(ex, "[BotRepository] Resolve update failed (ticker={Ticker})", ticker);
            }
        });
    }
}
