namespace KalshiBotWrapper.Data;

/// <summary>Row in the <c>event_history</c> table — one record per DashboardStore event.</summary>
public sealed class EventHistoryEntity
{
    public long     Id         { get; set; }
    public DateTime CreatedAt  { get; set; }
    public string   Kind       { get; set; } = "";
    /// <summary>JSON-serialized event payload (nullable — heartbeats have no payload).</summary>
    public string?  Payload    { get; set; }
    public string   KalshiEnv  { get; set; } = "";
    public bool     DryRun     { get; set; } = true;
}
