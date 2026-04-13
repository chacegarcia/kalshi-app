namespace KalshiBotWrapper.Data;

/// <summary>
/// Single-row settings table — always Id=1 (upserted).
/// Persists <see cref="Dashboard.BotControls"/> across container restarts.
/// </summary>
public sealed class BotControlsEntity
{
    /// <summary>Always 1 — singleton row.</summary>
    public int    Id                   { get; set; } = 1;
    public bool   ExecuteEnabled       { get; set; }
    public int    ScanIntervalSeconds  { get; set; } = 120;
    public int    MaxBetsPerHour       { get; set; } = 3;
    public double MaxHoursToClose      { get; set; } = 24.0;
    public int    NearFiftyMarginCents { get; set; } = 15;
    public int    MinPayoutMarginCents { get; set; } = 2;
    public int    SpendPerBetCents     { get; set; } = 500;
    public int    MaxOpenPositions     { get; set; } = 10;
    public DateTime UpdatedAt          { get; set; }
}
