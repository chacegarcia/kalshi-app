using System.Text.Json;
using System.Text.Json.Serialization;

namespace KalshiBotWrapper.Kalshi;

// ── Market models ─────────────────────────────────────────────────────────────

public sealed class Market
{
    [JsonPropertyName("ticker")]               public string  Ticker          { get; set; } = "";
    [JsonPropertyName("event_ticker")]         public string? EventTicker     { get; set; }
    [JsonPropertyName("title")]                public string  Title           { get; set; } = "";
    [JsonPropertyName("subtitle")]             public string? SubTitle        { get; set; }
    [JsonPropertyName("event_sub_title")]      public string? EventSubTitle   { get; set; }
    [JsonPropertyName("status")]               public string? Status          { get; set; }
    [JsonPropertyName("result")]               public string? Result          { get; set; } // "yes" | "no" | null
    [JsonPropertyName("close_time")]           public DateTimeOffset? CloseTime { get; set; }

    /// <summary>
    /// Builds the canonical Kalshi market URL.
    /// Full form: /markets/{event}/{slug}/{ticker} — requires an event subtitle to derive the slug.
    /// Falls back to the event page /markets/{event} when no subtitle is available.
    /// </summary>
    public string KalshiUrl
    {
        get
        {
            var ev   = (EventTicker ?? Ticker).ToLowerInvariant();
            var sub  = SubTitle ?? EventSubTitle;
            if (!string.IsNullOrWhiteSpace(sub))
            {
                var slug = SlugifyTitle(sub);
                if (!string.IsNullOrWhiteSpace(slug))
                    return $"https://kalshi.com/markets/{ev}/{slug}/{Ticker.ToLowerInvariant()}";
            }
            return $"https://kalshi.com/markets/{ev}";
        }
    }

    private static string SlugifyTitle(string s)
    {
        // lowercase, collapse non-alphanumeric runs to a single dash, trim edges
        var sb = new System.Text.StringBuilder();
        bool lastDash = true; // suppress leading dash
        foreach (var c in s.ToLowerInvariant())
        {
            if (char.IsLetterOrDigit(c)) { sb.Append(c); lastDash = false; }
            else if (!lastDash)          { sb.Append('-'); lastDash = true; }
        }
        // trim trailing dash
        if (sb.Length > 0 && sb[^1] == '-') sb.Length--;
        return sb.ToString();
    }

    // Kalshi returns prices as dollar strings ("0.4500" = 45¢)
    [JsonPropertyName("yes_bid_dollars")]      public string? YesBidDollars      { get; set; }
    [JsonPropertyName("yes_ask_dollars")]      public string? YesAskDollars      { get; set; }
    [JsonPropertyName("last_price_dollars")]   public string? LastPriceDollars   { get; set; }
    [JsonPropertyName("no_bid_dollars")]       public string? NoBidDollars       { get; set; }
    [JsonPropertyName("no_ask_dollars")]       public string? NoAskDollars       { get; set; }

    // Convenience: convert dollar string → cents integer (null / 0 = no price)
    public int? YesBid    => DollarsToCents(YesBidDollars);
    public int? YesAsk    => DollarsToCents(YesAskDollars);
    public int? LastPrice => DollarsToCents(LastPriceDollars);

    // Implied YES price from NO side (100 - no_ask = best yes bid backing; 100 - no_bid = best yes ask)
    public int? YesBidFromNo  => DollarsToCents(NoAskDollars)  is int v and > 0 ? 100 - v : null;
    public int? YesAskFromNo  => DollarsToCents(NoBidDollars)  is int v and > 0 ? 100 - v : null;

    private static int? DollarsToCents(string? dollars)
    {
        if (string.IsNullOrWhiteSpace(dollars)) return null;
        if (!double.TryParse(dollars,
                System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture,
                out var d) || d <= 0) return null;
        return (int)Math.Round(d * 100);
    }
}

public sealed class GetMarketsResponse
{
    [JsonPropertyName("markets")] public List<Market> Markets { get; set; } = [];
    [JsonPropertyName("cursor")] public string? Cursor { get; set; }
}

public sealed class GetMarketResponse
{
    [JsonPropertyName("market")] public Market? Market { get; set; }
}

// ── Orderbook models ──────────────────────────────────────────────────────────

/// <summary>
/// Each price level is [price_dollars, volume] as an array.
/// </summary>
public sealed class OrderbookFp
{
    [JsonPropertyName("yes")] public List<List<JsonElement>>? Yes { get; set; }
    [JsonPropertyName("no")] public List<List<JsonElement>>? No { get; set; }
}

public sealed class GetOrderbookResponse
{
    [JsonPropertyName("orderbook")] public OrderbookFp? Orderbook { get; set; }
}

// ── Portfolio models ──────────────────────────────────────────────────────────

public sealed class GetBalanceResponse
{
    [JsonPropertyName("balance")] public int? Balance { get; set; }
}

public sealed class MarketPosition
{
    [JsonPropertyName("ticker")] public string? Ticker { get; set; }

    // Kalshi prod API returns these as decimal strings with "_fp" / "_dollars" suffixes.
    // Keeping computed double? properties with the old names so all call sites stay unchanged.
    [JsonPropertyName("position_fp")]             public string? PositionFp            { get; set; }
    [JsonPropertyName("market_exposure_dollars")] public string? MarketExposureDollars { get; set; }
    [JsonPropertyName("total_traded_dollars")]    public string? TotalTradedDollars    { get; set; }
    [JsonPropertyName("realized_pnl_dollars")]    public string? RealizedPnlDollars    { get; set; }

    public double? Position       => ParseDouble(PositionFp);
    public double? MarketExposure => ParseDouble(MarketExposureDollars);
    public double? TotalTraded    => ParseDouble(TotalTradedDollars);
    public double? RealizedPnl    => ParseDouble(RealizedPnlDollars);

    private static double? ParseDouble(string? s)
    {
        if (string.IsNullOrWhiteSpace(s)) return null;
        return double.TryParse(s,
            System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture,
            out var v) ? v : null;
    }
}

public sealed class GetPositionsResponse
{
    [JsonPropertyName("market_positions")] public List<MarketPosition> MarketPositions { get; set; } = [];
    [JsonPropertyName("cursor")] public string? Cursor { get; set; }
}

// ── Order models ──────────────────────────────────────────────────────────────

public sealed class Order
{
    [JsonPropertyName("order_id")] public string? OrderId { get; set; }
    [JsonPropertyName("ticker")] public string? Ticker { get; set; }
    [JsonPropertyName("status")] public string? Status { get; set; }
    [JsonPropertyName("created_time")] public DateTimeOffset? CreatedTime { get; set; }
    [JsonPropertyName("side")] public string? Side { get; set; }
    [JsonPropertyName("action")] public string? Action { get; set; }
    [JsonPropertyName("count")] public int? Count { get; set; }
    [JsonPropertyName("yes_price")] public int? YesPrice { get; set; }
}

public sealed class GetOrdersResponse
{
    [JsonPropertyName("orders")] public List<Order> Orders { get; set; } = [];
    [JsonPropertyName("cursor")] public string? Cursor { get; set; }
}

public sealed class CreateOrderRequest
{
    [JsonPropertyName("ticker")] public string Ticker { get; set; } = "";
    [JsonPropertyName("client_order_id")] public string ClientOrderId { get; set; } = "";
    [JsonPropertyName("side")] public string Side { get; set; } = "";
    [JsonPropertyName("action")] public string Action { get; set; } = "";
    [JsonPropertyName("count")] public int Count { get; set; }
    [JsonPropertyName("yes_price")] public int YesPrice { get; set; }
    [JsonPropertyName("time_in_force")] public string TimeInForce { get; set; } = "good_till_canceled";
    [JsonPropertyName("type")] public string Type { get; set; } = "limit";
}

public sealed class CreateOrderResponse
{
    [JsonPropertyName("order")] public Order? Order { get; set; }
}

public sealed class BatchCancelRequest
{
    [JsonPropertyName("ids")] public List<string> Ids { get; set; } = [];
}

public sealed class BatchCancelResponse
{
    [JsonPropertyName("order_ids")] public List<string>? OrderIds { get; set; }
}
