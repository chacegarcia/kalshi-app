using System.Reflection;

namespace KalshiBotWrapper.Bot;

/// <summary>
/// All trading settings loaded from environment variables (Bot__ prefix via appsettings or raw env).
/// Mirrors Python config.py Settings class.
/// </summary>
public sealed class TradingSettings
{
    // ── Kalshi auth ────────────────────────────────────────────────────────────
    public string KalshiApiKeyId { get; init; } = "";
    public string? KalshiPrivateKeyPath { get; init; }
    public string? KalshiPrivateKeyPem { get; init; }
    public string KalshiEnv { get; init; } = "demo"; // "demo" | "prod"
    public string? KalshiRestBaseUrl { get; init; }
    public string? KalshiWsUrl { get; init; }

    // ── Trading mode ───────────────────────────────────────────────────────────
    public bool LiveTrading { get; init; } = false;
    public bool DryRun { get; init; } = true;

    // ── Risk & bankroll ────────────────────────────────────────────────────────
    public double MaxExposureCents { get; init; } = 50_000.0;
    public int MaxContractsPerMarket { get; init; } = 10;
    public double MaxDailyDrawdownUsd { get; init; } = 25.0;
    public int MaxOpenOrdersPerMarket { get; init; } = 3;
    public int CooldownAfterLossSeconds { get; init; } = 300;
    public int LossStreakThreshold { get; init; } = 3;
    public int CooldownAfterLossStreakSeconds { get; init; } = 900;
    public bool NoMartingale { get; init; } = true;
    public int StaleOrderSeconds { get; init; } = 3600;
    public bool KillSwitch { get; init; } = false;

    // ── Strategy ───────────────────────────────────────────────────────────────
    public string StrategyMarketTicker { get; init; } = "";
    public double StrategyMaxYesAskDollars { get; init; } = 0.55;
    public double StrategyMinSpreadDollars { get; init; } = 0.0;
    public double StrategyProbabilityGap { get; init; } = 0.0;
    public int StrategyOrderCount { get; init; } = 1;
    public double? TradeMaxOrderNotionalUsd { get; init; }
    public int StrategyLimitPriceCents { get; init; } = 50;
    public int StrategyMinSecondsBetweenSignals { get; init; } = 45;

    // ── Edge strategy ──────────────────────────────────────────────────────────
    public double? TradeFairYesProb { get; init; }
    public bool TradeUseEdgeStrategy { get; init; } = false;
    public double TradeMinNetEdgeAfterFees { get; init; } = 0.025;
    public double TradeEdgeMiddleExtraEdge { get; init; } = 0.01;

    // ── LLM screen ─────────────────────────────────────────────────────────────
    public bool TradeLlmScreenEnabled { get; init; } = false;
    public string TradeLlmModel { get; init; } = "gpt-4o-mini";
    public string? OpenAiApiKey { get; init; }
    public bool TradeLlmAutoExecute { get; init; } = false;
    public int TradeLlmMaxMarketsPerRun { get; init; } = 20;
    public bool TradeLlmRelaxedApproval { get; init; } = false;

    // ── Balance-scaled limits ──────────────────────────────────────────────────
    public bool TradeBalanceSizingEnabled { get; init; } = false;
    public double TradeRiskPctOfBalancePerTrade { get; init; } = 0.02;
    public double TradeTotalRiskPctOfBalance { get; init; } = 0.25;

    // ── Auto-sell / exit ───────────────────────────────────────────────────────
    public int? AutoSellMinYesBidCents { get; init; }
    public double AutoSellPollSeconds { get; init; } = 2.0;
    public double TradeExitTakeProfitMinYesBidPct { get; init; } = 72.0;
    public double? TradeExitMinProfitCentsPerContract { get; init; }
    public int? TradeExitEntryReferenceYesCents { get; init; }
    public bool TradeExitEstimateEntryFromPortfolio { get; init; } = true;
    public bool TradeExitOnlyProfitMargin { get; init; } = false;
    public string TradeExitSellTimeInForce { get; init; } = "immediate_or_cancel";
    public int TradeExitSellAggressionCents { get; init; } = 0;

    // ── Paper / backtest ───────────────────────────────────────────────────────
    public double PaperFeeCentsPerContract { get; init; } = 0.0;
    public double PaperSlippageCentsPerContract { get; init; } = 0.0;
    public double PaperFillProbability { get; init; } = 0.85;

    // ── Dashboard ──────────────────────────────────────────────────────────────
    public bool DashboardEnabled { get; init; } = true;
    public string LogLevel { get; init; } = "INFO";

    // ── Derived properties ─────────────────────────────────────────────────────
    public string RestBaseUrl =>
        !string.IsNullOrWhiteSpace(KalshiRestBaseUrl) ? KalshiRestBaseUrl.TrimEnd('/') :
        KalshiEnv == "demo"
            ? "https://demo-api.kalshi.co/trade-api/v2"
            : "https://api.elections.kalshi.com/trade-api/v2";

    public string WsUrl =>
        !string.IsNullOrWhiteSpace(KalshiWsUrl) ? KalshiWsUrl.TrimEnd('/') :
        KalshiEnv == "demo"
            ? "wss://demo-api.kalshi.co/trade-api/ws/v2"
            : "wss://api.elections.kalshi.com/trade-api/ws/v2";

    public bool CanSendRealOrders => LiveTrading && !DryRun;

    public double TradeBuyMaxYesAskImpliedPct => StrategyMaxYesAskDollars * 100.0;
    public double TradeEntryMinEdgeFrom50PctPoints => StrategyProbabilityGap * 100.0;

    public int? AutoSellEffectiveMinYesBidCents(int? cliOverride)
    {
        if (TradeExitOnlyProfitMargin) return null;
        if (cliOverride.HasValue) return cliOverride;
        if (AutoSellMinYesBidCents.HasValue) return AutoSellMinYesBidCents;
        return (int)Math.Round(TradeExitTakeProfitMinYesBidPct);
    }

    // ── Load from environment variables ───────────────────────────────────────
    public static TradingSettings FromEnvironment()
    {
        return new TradingSettings
        {
            KalshiApiKeyId = Env("KALSHI_API_KEY_ID", ""),
            KalshiPrivateKeyPath = EnvOrNull("KALSHI_PRIVATE_KEY_PATH"),
            KalshiPrivateKeyPem = EnvOrNull("KALSHI_PRIVATE_KEY_PEM"),
            KalshiEnv = Env("KALSHI_ENV", "demo"),
            KalshiRestBaseUrl = EnvOrNull("KALSHI_REST_BASE_URL"),
            KalshiWsUrl = EnvOrNull("KALSHI_WS_URL"),

            LiveTrading = EnvBool("LIVE_TRADING", false),
            DryRun = EnvBool("DRY_RUN", true),

            MaxExposureCents = EnvDouble("TRADE_MAX_TOTAL_EXPOSURE_CENTS",
                EnvDouble("MAX_EXPOSURE_CENTS", 50_000.0)),
            MaxContractsPerMarket = EnvInt("TRADE_MAX_CONTRACTS_PER_MARKET",
                EnvInt("MAX_CONTRACTS_PER_MARKET", 10)),
            MaxDailyDrawdownUsd = EnvDouble("TRADE_STOP_MAX_SESSION_LOSS_USD",
                EnvDouble("MAX_DAILY_DRAWDOWN_USD", 25.0)),
            MaxOpenOrdersPerMarket = EnvInt("MAX_OPEN_ORDERS_PER_MARKET", 3),
            CooldownAfterLossSeconds = EnvInt("COOLDOWN_AFTER_LOSS_SECONDS", 300),
            LossStreakThreshold = EnvInt("LOSS_STREAK_THRESHOLD", 3),
            CooldownAfterLossStreakSeconds = EnvInt("COOLDOWN_AFTER_LOSS_STREAK_SECONDS", 900),
            NoMartingale = EnvBool("NO_MARTINGALE", true),
            StaleOrderSeconds = EnvInt("STALE_ORDER_SECONDS", 3600),
            KillSwitch = EnvBool("KILL_SWITCH", false),

            StrategyMarketTicker = Env("TRADE_MARKET_TICKER", Env("STRATEGY_MARKET_TICKER", "")),
            StrategyMaxYesAskDollars = EnvDouble("TRADE_BUY_MAX_YES_ASK_DOLLARS",
                EnvDouble("STRATEGY_MAX_YES_ASK_DOLLARS", 0.55)),
            StrategyMinSpreadDollars = EnvDouble("TRADE_ENTRY_MIN_SPREAD_DOLLARS",
                EnvDouble("STRATEGY_MIN_SPREAD_DOLLARS", 0.0)),
            StrategyProbabilityGap = EnvDouble("TRADE_ENTRY_MIN_EDGE_FROM_50",
                EnvDouble("STRATEGY_PROBABILITY_GAP", 0.0)),
            StrategyOrderCount = EnvInt("TRADE_BUY_CONTRACTS_PER_ORDER",
                EnvInt("STRATEGY_ORDER_COUNT", 1)),
            TradeMaxOrderNotionalUsd = EnvDoubleOrNull("TRADE_MAX_ORDER_NOTIONAL_USD"),
            StrategyLimitPriceCents = EnvInt("TRADE_BUY_LIMIT_YES_PRICE_CENTS",
                EnvInt("STRATEGY_LIMIT_PRICE_CENTS", 50)),
            StrategyMinSecondsBetweenSignals = EnvInt("TRADE_MIN_SECONDS_BETWEEN_ORDERS",
                EnvInt("STRATEGY_MIN_SECONDS_BETWEEN_SIGNALS", 45)),

            TradeFairYesProb = EnvDoubleOrNull("TRADE_FAIR_YES_PROB"),
            TradeUseEdgeStrategy = EnvBool("TRADE_USE_EDGE_STRATEGY", false),
            TradeMinNetEdgeAfterFees = EnvDouble("TRADE_MIN_NET_EDGE_AFTER_FEES", 0.025),
            TradeEdgeMiddleExtraEdge = EnvDouble("TRADE_EDGE_MIDDLE_EXTRA_EDGE", 0.01),

            TradeLlmScreenEnabled = EnvBool("TRADE_LLM_SCREEN_ENABLED", false),
            TradeLlmModel = Env("TRADE_LLM_MODEL", "gpt-4o-mini"),
            OpenAiApiKey = EnvOrNull("OPENAI_API_KEY"),
            TradeLlmAutoExecute = EnvBool("TRADE_LLM_AUTO_EXECUTE", false),
            TradeLlmMaxMarketsPerRun = EnvInt("TRADE_LLM_MAX_MARKETS_PER_RUN", 20),
            TradeLlmRelaxedApproval = EnvBool("TRADE_LLM_RELAXED_APPROVAL", false),

            TradeBalanceSizingEnabled = EnvBool("TRADE_BALANCE_SIZING_ENABLED", false),
            TradeRiskPctOfBalancePerTrade = EnvDouble("TRADE_RISK_PCT_OF_BALANCE_PER_TRADE", 0.02),
            TradeTotalRiskPctOfBalance = EnvDouble("TRADE_TOTAL_RISK_PCT_OF_BALANCE", 0.25),

            AutoSellMinYesBidCents = EnvIntOrNull("TRADE_TAKE_PROFIT_MIN_YES_BID_CENTS") ??
                                     EnvIntOrNull("AUTO_SELL_MIN_YES_BID_CENTS"),
            AutoSellPollSeconds = EnvDouble("TRADE_TAKE_PROFIT_POLL_SECONDS",
                EnvDouble("AUTO_SELL_POLL_SECONDS", 2.0)),
            TradeExitTakeProfitMinYesBidPct = EnvDouble("TRADE_EXIT_TAKE_PROFIT_MIN_YES_BID_PCT", 72.0),
            TradeExitMinProfitCentsPerContract = EnvDoubleOrNull("TRADE_EXIT_MIN_PROFIT_CENTS_PER_CONTRACT") ??
                                                 EnvDoubleOrNull("TRADE_EXIT_MIN_PROFIT_CENTS"),
            TradeExitEntryReferenceYesCents = EnvIntOrNull("TRADE_EXIT_ENTRY_REFERENCE_YES_CENTS"),
            TradeExitEstimateEntryFromPortfolio = EnvBool("TRADE_EXIT_ESTIMATE_ENTRY_FROM_PORTFOLIO", true),
            TradeExitOnlyProfitMargin = EnvBool("TRADE_EXIT_ONLY_PROFIT_MARGIN", false),
            TradeExitSellTimeInForce = NormalizeTif(Env("TRADE_EXIT_SELL_TIME_IN_FORCE", "immediate_or_cancel")),
            TradeExitSellAggressionCents = EnvInt("TRADE_EXIT_SELL_AGGRESSION_CENTS", 0),

            PaperFeeCentsPerContract = EnvDouble("PAPER_FEE_CENTS_PER_CONTRACT", 0.0),
            PaperSlippageCentsPerContract = EnvDouble("PAPER_SLIPPAGE_CENTS_PER_CONTRACT", 0.0),
            PaperFillProbability = EnvDouble("PAPER_FILL_PROBABILITY", 0.85),

            DashboardEnabled = EnvBool("DASHBOARD_ENABLED", true),
            LogLevel = Env("LOG_LEVEL", "INFO"),
        };
    }

    private static string Env(string key, string def)
    {
        var v = Environment.GetEnvironmentVariable(key);
        return string.IsNullOrWhiteSpace(v) ? def : v.Trim();
    }

    private static string? EnvOrNull(string key)
    {
        var v = Environment.GetEnvironmentVariable(key);
        return string.IsNullOrWhiteSpace(v) ? null : v.Trim();
    }

    private static bool EnvBool(string key, bool def)
    {
        var v = Environment.GetEnvironmentVariable(key);
        if (string.IsNullOrWhiteSpace(v)) return def;
        return v.Trim().ToLowerInvariant() is "1" or "true" or "yes" or "on";
    }

    private static int EnvInt(string key, int def)
    {
        var v = Environment.GetEnvironmentVariable(key);
        return int.TryParse(v?.Trim(), out var r) ? r : def;
    }

    private static int? EnvIntOrNull(string key)
    {
        var v = Environment.GetEnvironmentVariable(key);
        return int.TryParse(v?.Trim(), out var r) ? r : null;
    }

    private static double EnvDouble(string key, double def)
    {
        var v = Environment.GetEnvironmentVariable(key);
        return double.TryParse(v?.Trim(), System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture, out var r) ? r : def;
    }

    private static double? EnvDoubleOrNull(string key)
    {
        var v = Environment.GetEnvironmentVariable(key);
        if (string.IsNullOrWhiteSpace(v)) return null;
        return double.TryParse(v.Trim(), System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture, out var r) ? r : null;
    }

    private static string NormalizeTif(string s)
    {
        s = s.Trim().ToLowerInvariant().Replace("-", "_");
        return s switch
        {
            "ioc" => "immediate_or_cancel",
            "fok" => "fill_or_kill",
            "gtc" => "good_till_canceled",
            "good_till_cancelled" => "good_till_canceled",
            _ => s
        };
    }
}
