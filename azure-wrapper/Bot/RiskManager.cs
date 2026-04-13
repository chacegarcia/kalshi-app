namespace KalshiBotWrapper.Bot;

/// <summary>
/// Pre-trade risk checks: exposure, drawdown, streak cooldown, kill switch, anti-martingale.
/// Mirrors Python risk.py RiskManager.
/// </summary>
public sealed class RiskManager
{
    private readonly TradingSettings _s;

    // Session state
    private int? _sessionStartBalanceCents;
    private int? _lastBalanceCents;
    private DateTimeOffset _cooldownUntil = DateTimeOffset.MinValue;
    private int _consecutiveLosses;
    private int? _lastOrderContracts;
    private bool _lastRealizedLoss;

    public RiskManager(TradingSettings settings) => _s = settings;

    public bool KillSwitchActive => _s.KillSwitch;
    public bool InCooldown => DateTimeOffset.UtcNow < _cooldownUntil;

    public double DailyLossUsd()
    {
        if (_sessionStartBalanceCents is null || _lastBalanceCents is null) return 0.0;
        var dd = _sessionStartBalanceCents.Value - _lastBalanceCents.Value;
        return Math.Max(0.0, dd / 100.0);
    }

    public void RecordBalanceSample(int? balanceCents)
    {
        if (balanceCents is null) return;
        if (_sessionStartBalanceCents is null)
        {
            _sessionStartBalanceCents = balanceCents;
            _lastBalanceCents = balanceCents;
            return;
        }

        var prev = _lastBalanceCents ?? balanceCents.Value;
        _lastBalanceCents = balanceCents;

        var ddCents = (_sessionStartBalanceCents ?? balanceCents.Value) - balanceCents.Value;
        if (ddCents > (int)(_s.MaxDailyDrawdownUsd * 100)) return;

        var lossStepCents = prev - balanceCents.Value;
        if (lossStepCents >= 1 && _s.CooldownAfterLossSeconds > 0)
            _cooldownUntil = DateTimeOffset.UtcNow.AddSeconds(_s.CooldownAfterLossSeconds);
    }

    public void RecordClosedTrade(double pnlCents)
    {
        if (pnlCents < 0)
        {
            _consecutiveLosses++;
            _lastRealizedLoss = true;
        }
        else
        {
            _consecutiveLosses = 0;
            _lastRealizedLoss = false;
        }

        if (_consecutiveLosses >= _s.LossStreakThreshold && _s.CooldownAfterLossStreakSeconds > 0)
        {
            var candidate = DateTimeOffset.UtcNow.AddSeconds(_s.CooldownAfterLossStreakSeconds);
            if (candidate > _cooldownUntil) _cooldownUntil = candidate;
        }
    }

    public (bool Allowed, string Reason) CheckNewOrder(
        string marketTicker,
        int orderContracts,
        double projectedAbsPosition,
        int restingOrdersOnMarket,
        double currentTotalExposureCents = 0.0,
        double additionalOrderExposureCents = 0.0,
        bool orderIncreasesExposure = true,
        int? maxContractsOverride = null,
        double? maxExposureCentsOverride = null)
    {
        if (KillSwitchActive) return (false, "kill_switch_enabled");
        if (InCooldown) return (false, "cooldown");
        if (DailyLossUsd() >= _s.MaxDailyDrawdownUsd) return (false, "max_daily_drawdown_exceeded");

        var maxC = maxContractsOverride ?? _s.MaxContractsPerMarket;
        if (projectedAbsPosition > maxC) return (false, "max_contracts_per_market_exceeded");

        if (restingOrdersOnMarket >= _s.MaxOpenOrdersPerMarket) return (false, "max_open_orders_per_market");

        var maxExp = maxExposureCentsOverride ?? _s.MaxExposureCents;
        if (currentTotalExposureCents + additionalOrderExposureCents > maxExp) return (false, "max_exposure_exceeded");

        // Anti-martingale: do not increase buy size after a loss
        if (_s.NoMartingale && orderIncreasesExposure && _lastRealizedLoss && _lastOrderContracts.HasValue)
        {
            if (orderContracts > _lastOrderContracts.Value)
                return (false, "no_martingale_increase_after_loss");
        }

        return (true, "ok");
    }

    public void RecordOrderSubmitted(int orderContracts)
        => _lastOrderContracts = orderContracts;
}
