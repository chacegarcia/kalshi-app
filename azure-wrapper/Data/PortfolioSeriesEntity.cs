namespace KalshiBotWrapper.Data;

/// <summary>Row in the <c>portfolio_series</c> table — one point per scan cycle.</summary>
public sealed class PortfolioSeriesEntity
{
    public long     Id            { get; set; }
    public DateTime RecordedAt    { get; set; }
    public int?     BalanceCents  { get; set; }
    public int      ContractCount { get; set; }
    public int      BetsPlaced    { get; set; }
    public int      SpentCents    { get; set; }
    public string   KalshiEnv    { get; set; } = "";
    public bool     DryRun       { get; set; } = true;
}
