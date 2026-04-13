using System.Net;
using System.Net.Http.Json;
using System.Text.Json;

namespace KalshiBotWrapper.Kalshi;

/// <summary>
/// Authenticated Kalshi REST client. Retries on transient network / 5xx errors.
/// Mirrors Python KalshiSdkClient + with_rest_retry.
/// </summary>
public sealed class KalshiRestClient : IDisposable
{
    private readonly HttpClient _http;
    private readonly KalshiAuth _auth;
    private readonly string _baseUrl;

    private static readonly JsonSerializerOptions _json = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    public KalshiRestClient(string restBaseUrl, KalshiAuth auth)
    {
        _baseUrl = restBaseUrl.TrimEnd('/');
        _auth = auth;
        _http = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(30),
        };
    }

    // ── Markets ───────────────────────────────────────────────────────────────

    public async Task<GetMarketsResponse> GetMarketsAsync(
        string status = "open",
        int limit = 200,
        string? mveFilter = "exclude",
        string? cursor = null,
        CancellationToken ct = default)
    {
        var qs = $"?status={Uri.EscapeDataString(status)}&limit={limit}";
        if (mveFilter != null) qs += $"&mve_filter={Uri.EscapeDataString(mveFilter)}";
        if (cursor   != null) qs += $"&cursor={Uri.EscapeDataString(cursor)}";
        return await GetAsync<GetMarketsResponse>($"/markets{qs}", ct) ?? new();
    }

    /// <summary>Fetches ALL open markets by walking the cursor until exhausted.</summary>
    public async Task<List<Market>> GetAllMarketsAsync(
        string status = "open",
        string? mveFilter = "exclude",
        CancellationToken ct = default)
    {
        var all = new List<Market>();
        string? cursor = null;
        do
        {
            var page = await GetMarketsAsync(status, 200, mveFilter, cursor, ct);
            all.AddRange(page.Markets);
            cursor = string.IsNullOrEmpty(page.Cursor) ? null : page.Cursor;
        }
        while (cursor != null);
        return all;
    }

    public async Task<GetMarketResponse> GetMarketAsync(string ticker, CancellationToken ct = default)
        => await GetAsync<GetMarketResponse>($"/markets/{Uri.EscapeDataString(ticker)}", ct) ?? new();

    /// <summary>Returns the raw JSON of the first market on page 1 for diagnostics.</summary>
    public async Task<string> GetRawFirstMarketJsonAsync(CancellationToken ct = default)
    {
        return await WithRetryAsync(async () =>
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, _baseUrl + "/markets?status=open&limit=1");
            AddAuthHeaders(req, "GET", "/markets");
            using var resp = await _http.SendAsync(req, ct);
            await EnsureSuccessAsync(resp);
            return await resp.Content.ReadAsStringAsync(ct);
        }, ct) ?? "(null)";
    }

    public async Task<GetOrderbookResponse> GetOrderbookAsync(string ticker, int depth = 10, CancellationToken ct = default)
        => await GetAsync<GetOrderbookResponse>(
            $"/markets/{Uri.EscapeDataString(ticker)}/orderbook?depth={depth}", ct) ?? new();

    // ── Portfolio ─────────────────────────────────────────────────────────────

    public async Task<GetBalanceResponse> GetBalanceAsync(CancellationToken ct = default)
        => await GetAsync<GetBalanceResponse>("/portfolio/balance", ct) ?? new();

    /// <summary>Returns raw positions JSON for diagnostic purposes.</summary>
    public async Task<string> GetRawPositionsJsonAsync(int limit = 500, CancellationToken ct = default)
    {
        return await WithRetryAsync(async () =>
        {
            var path = $"/portfolio/positions?limit={limit}&count_filter=position";
            using var req = new HttpRequestMessage(HttpMethod.Get, _baseUrl + path);
            AddAuthHeaders(req, "GET", "/portfolio/positions");
            using var resp = await _http.SendAsync(req, ct);
            await EnsureSuccessAsync(resp);
            return await resp.Content.ReadAsStringAsync(ct);
        }, ct) ?? "(null)";
    }

    public async Task<GetPositionsResponse> GetPositionsAsync(
        string? ticker = null,
        string? countFilter = "position",
        int limit = 500,
        CancellationToken ct = default)
    {
        var qs = $"?limit={limit}";
        if (!string.IsNullOrWhiteSpace(ticker)) qs += $"&ticker={Uri.EscapeDataString(ticker)}";
        if (!string.IsNullOrWhiteSpace(countFilter)) qs += $"&count_filter={Uri.EscapeDataString(countFilter)}";
        return await GetAsync<GetPositionsResponse>($"/portfolio/positions{qs}", ct) ?? new();
    }

    // ── Orders ────────────────────────────────────────────────────────────────

    public async Task<GetOrdersResponse> GetOrdersAsync(
        string status = "resting",
        string? ticker = null,
        int limit = 200,
        string? cursor = null,
        CancellationToken ct = default)
    {
        var qs = $"?status={Uri.EscapeDataString(status)}&limit={limit}";
        if (!string.IsNullOrWhiteSpace(ticker)) qs += $"&ticker={Uri.EscapeDataString(ticker)}";
        if (!string.IsNullOrWhiteSpace(cursor)) qs += $"&cursor={Uri.EscapeDataString(cursor)}";
        return await GetAsync<GetOrdersResponse>($"/portfolio/orders{qs}", ct) ?? new();
    }

    public async Task<CreateOrderResponse> CreateOrderAsync(CreateOrderRequest req, CancellationToken ct = default)
        => await PostAsync<CreateOrderRequest, CreateOrderResponse>("/portfolio/orders", req, ct) ?? new();

    public async Task<BatchCancelResponse> BatchCancelOrdersAsync(IEnumerable<string> ids, CancellationToken ct = default)
    {
        var body = new BatchCancelRequest { Ids = ids.ToList() };
        return await DeleteWithBodyAsync<BatchCancelRequest, BatchCancelResponse>(
            "/portfolio/orders/batched", body, ct) ?? new();
    }

    // ── HTTP helpers ──────────────────────────────────────────────────────────

    private async Task<T?> GetAsync<T>(string path, CancellationToken ct) where T : class
    {
        return await WithRetryAsync(async () =>
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, _baseUrl + path);
            AddAuthHeaders(req, "GET", path.Split('?')[0]);
            using var resp = await _http.SendAsync(req, ct);
            await EnsureSuccessAsync(resp);
            return await resp.Content.ReadFromJsonAsync<T>(_json, ct);
        }, ct);
    }

    private async Task<TResponse?> PostAsync<TRequest, TResponse>(
        string path, TRequest body, CancellationToken ct) where TResponse : class
    {
        return await WithRetryAsync(async () =>
        {
            using var req = new HttpRequestMessage(HttpMethod.Post, _baseUrl + path);
            req.Content = JsonContent.Create(body, options: _json);
            AddAuthHeaders(req, "POST", path);
            using var resp = await _http.SendAsync(req, ct);
            await EnsureSuccessAsync(resp);
            return await resp.Content.ReadFromJsonAsync<TResponse>(_json, ct);
        }, ct);
    }

    private async Task<TResponse?> DeleteWithBodyAsync<TRequest, TResponse>(
        string path, TRequest body, CancellationToken ct) where TResponse : class
    {
        return await WithRetryAsync(async () =>
        {
            using var req = new HttpRequestMessage(HttpMethod.Delete, _baseUrl + path);
            req.Content = JsonContent.Create(body, options: _json);
            AddAuthHeaders(req, "DELETE", path);
            using var resp = await _http.SendAsync(req, ct);
            await EnsureSuccessAsync(resp);
            return await resp.Content.ReadFromJsonAsync<TResponse>(_json, ct);
        }, ct);
    }

    private void AddAuthHeaders(HttpRequestMessage req, string method, string path)
    {
        // Strip query string for signing (same as Python SDK)
        var pathForSign = path.Split('?')[0];
        // Convert /trade-api/v2/markets to full WS-style path for signing
        // Kalshi signs the path starting with /trade-api/v2
        var signPath = "/trade-api/v2" + pathForSign;
        var headers = _auth.CreateAuthHeaders(method, signPath);
        foreach (var (k, v) in headers)
            req.Headers.TryAddWithoutValidation(k, v);
        req.Headers.TryAddWithoutValidation("Content-Type", "application/json");
    }

    private static async Task EnsureSuccessAsync(HttpResponseMessage resp)
    {
        if (!resp.IsSuccessStatusCode)
        {
            var body = await resp.Content.ReadAsStringAsync();
            throw new KalshiApiException((int)resp.StatusCode, body);
        }
    }

    private static async Task<T?> WithRetryAsync<T>(Func<Task<T?>> fn, CancellationToken ct)
    {
        const int maxAttempts = 5;
        var delay = TimeSpan.FromMilliseconds(500);
        Exception? last = null;
        for (int i = 0; i < maxAttempts; i++)
        {
            ct.ThrowIfCancellationRequested();
            try
            {
                return await fn();
            }
            catch (KalshiApiException ex) when (ex.StatusCode is >= 500 or 429)
            {
                last = ex;
            }
            catch (HttpRequestException ex)
            {
                last = ex;
            }
            catch (TaskCanceledException) when (!ct.IsCancellationRequested)
            {
                last = new TimeoutException("Kalshi request timed out");
            }

            if (i < maxAttempts - 1)
            {
                await Task.Delay(delay, ct);
                delay = TimeSpan.FromSeconds(Math.Min(delay.TotalSeconds * 2 + Random.Shared.NextDouble(), 20));
            }
        }
        throw last!;
    }

    public void Dispose() => _http.Dispose();
}

public sealed class KalshiApiException : Exception
{
    public int StatusCode { get; }
    public KalshiApiException(int statusCode, string body)
        : base($"Kalshi API {statusCode}: {body}")
    {
        StatusCode = statusCode;
    }
}
