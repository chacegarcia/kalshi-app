namespace KalshiBotWrapper.Data;

/// <summary>
/// Row in the <c>suggestion_history</c> table — one record per <see cref="Dashboard.SuggestionRecord"/>.
/// Inserted when the record is first created; updated in-place when executed or resolved.
/// </summary>
public sealed class SuggestionHistoryEntity
{
    public long            Id             { get; set; }
    public DateTime        CreatedAt      { get; set; }
    /// <summary>Matches <see cref="Dashboard.SuggestionRecord.Id"/>.</summary>
    public Guid            SuggestionId   { get; set; }
    public string          Ticker         { get; set; } = "";
    public string          EventTicker    { get; set; } = "";
    public string          Title          { get; set; } = "";
    public int             YesAskCents    { get; set; }
    public int             MidCents       { get; set; }
    public int             ContractCount  { get; set; }
    public int             SpendCents     { get; set; }
    public DateTimeOffset  SuggestedAt    { get; set; }
    public DateTimeOffset  CloseTime      { get; set; }
    public int             ScanRank       { get; set; }
    public string          Url            { get; set; } = "";
    public bool            Executed       { get; set; }
    public DateTimeOffset? ExecutedAt     { get; set; }
    public string?         ExecuteError   { get; set; }
    /// <summary>"yes" | "no" | null when unresolved.</summary>
    public string?         Resolution     { get; set; }
    /// <summary>Projected P&amp;L in cents once resolved.</summary>
    public int?            OutcomeCents   { get; set; }
    public string          KalshiEnv      { get; set; } = "";
    public bool            DryRun         { get; set; } = true;
}
