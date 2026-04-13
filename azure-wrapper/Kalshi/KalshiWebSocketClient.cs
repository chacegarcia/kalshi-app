using System.Net.WebSockets;
using System.Text;
using System.Text.Json;

namespace KalshiBotWrapper.Kalshi;

/// <summary>
/// Authenticated Kalshi WebSocket client with reconnect + exponential backoff.
/// Mirrors Python KalshiWS.
/// </summary>
public sealed class KalshiWebSocketClient
{
    private readonly string _wsUrl;
    private readonly KalshiAuth _auth;
    private readonly Func<JsonElement, Task> _onMessage;
    private readonly double _maxBackoffSeconds;
    private int _msgId = 1;

    public KalshiWebSocketClient(
        string wsUrl,
        KalshiAuth auth,
        Func<JsonElement, Task> onMessage,
        double maxBackoffSeconds = 60.0)
    {
        _wsUrl = wsUrl;
        _auth = auth;
        _onMessage = onMessage;
        _maxBackoffSeconds = maxBackoffSeconds;
    }

    /// <summary>
    /// Connect, subscribe, receive messages, reconnect on failure — loops forever until cancellation.
    /// </summary>
    public async Task RunAsync(IReadOnlyList<string> marketTickers, CancellationToken ct)
    {
        int attempt = 0;
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await ConnectAndRunAsync(marketTickers, ct);
                attempt = 0;
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                return;
            }
            catch
            {
                attempt++;
                var exp = Math.Min(_maxBackoffSeconds, Math.Pow(2, Math.Min(attempt, 8)) * 0.25);
                var jitter = Random.Shared.NextDouble() * 0.5;
                var delay = TimeSpan.FromSeconds(exp + jitter);
                try { await Task.Delay(delay, ct); } catch (OperationCanceledException) { return; }
            }
        }
    }

    private async Task ConnectAndRunAsync(IReadOnlyList<string> marketTickers, CancellationToken ct)
    {
        using var ws = new ClientWebSocket();

        // Add auth headers to handshake
        var headers = _auth.WebSocketHandshakeHeaders();
        foreach (var (k, v) in headers)
            ws.Options.SetRequestHeader(k, v);

        await ws.ConnectAsync(new Uri(_wsUrl), ct);

        // Subscribe to ticker channel (all markets)
        await SendAsync(ws, new
        {
            id = NextId(),
            cmd = "subscribe",
            @params = new { channels = new[] { "ticker" } }
        }, ct);

        // Subscribe to orderbook_delta for specific tickers if any
        if (marketTickers.Count > 0)
        {
            await SendAsync(ws, new
            {
                id = NextId(),
                cmd = "subscribe",
                @params = new { channels = new[] { "orderbook_delta" }, market_tickers = marketTickers }
            }, ct);
        }

        // Receive loop
        var buffer = new byte[64 * 1024];
        var sb = new StringBuilder();
        while (ws.State == WebSocketState.Open && !ct.IsCancellationRequested)
        {
            sb.Clear();
            WebSocketReceiveResult result;
            do
            {
                result = await ws.ReceiveAsync(buffer, ct);
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "", ct);
                    return;
                }
                sb.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
            } while (!result.EndOfMessage);

            try
            {
                using var doc = JsonDocument.Parse(sb.ToString());
                await _onMessage(doc.RootElement.Clone());
            }
            catch (JsonException)
            {
                // skip malformed message
            }
        }
    }

    private async Task SendAsync<T>(ClientWebSocket ws, T payload, CancellationToken ct)
    {
        var json = JsonSerializer.Serialize(payload);
        var bytes = Encoding.UTF8.GetBytes(json);
        await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, ct);
    }

    private int NextId() => Interlocked.Increment(ref _msgId);
}
