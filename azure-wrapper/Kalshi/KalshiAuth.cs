using System.Security.Cryptography;
using System.Text;

namespace KalshiBotWrapper.Kalshi;

/// <summary>
/// RSA-PSS SHA-256 request signing for the Kalshi trade API.
/// Mirrors Python kalshi_python_sync.auth.KalshiAuth.
/// </summary>
public sealed class KalshiAuth
{
    private readonly string _keyId;
    private readonly RSA _rsa;

    public KalshiAuth(string apiKeyId, string pemText)
    {
        if (string.IsNullOrWhiteSpace(apiKeyId))
            throw new ArgumentException("API key ID must not be empty.", nameof(apiKeyId));

        _keyId = apiKeyId.Trim();
        _rsa = RSA.Create();

        // Allow literal \n in env strings (same as Python load_private_key_pem)
        var pem = pemText.Replace("\\n", "\n").Trim();
        _rsa.ImportFromPem(pem);
    }

    /// <summary>
    /// Build the auth headers required by Kalshi REST endpoints.
    /// </summary>
    public Dictionary<string, string> CreateAuthHeaders(string method, string path)
    {
        var tsMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString();
        var msgBytes = Encoding.UTF8.GetBytes(tsMs + method.ToUpperInvariant() + path);
        var sigBytes = _rsa.SignData(
            msgBytes,
            HashAlgorithmName.SHA256,
            RSASignaturePadding.Pss);
        var sig = Convert.ToBase64String(sigBytes);

        return new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["KALSHI-ACCESS-KEY"] = _keyId,
            ["KALSHI-ACCESS-TIMESTAMP"] = tsMs,
            ["KALSHI-ACCESS-SIGNATURE"] = sig,
        };
    }

    /// <summary>
    /// Headers for the WebSocket handshake GET /trade-api/ws/v2.
    /// </summary>
    public Dictionary<string, string> WebSocketHandshakeHeaders()
    {
        var h = CreateAuthHeaders("GET", "/trade-api/ws/v2");
        h["Content-Type"] = "application/json";
        return h;
    }
}

public sealed class AuthError : Exception
{
    public AuthError(string message) : base(message) { }
}

public static class KalshiAuthLoader
{
    /// <summary>
    /// Load private key PEM from file path or inline PEM string, then build KalshiAuth.
    /// </summary>
    public static KalshiAuth Build(string apiKeyId, string? keyPath, string? keyPem)
    {
        if (string.IsNullOrWhiteSpace(apiKeyId))
            throw new AuthError("KALSHI_API_KEY_ID is required");

        string pem;
        if (!string.IsNullOrWhiteSpace(keyPath))
        {
            var p = Path.GetFullPath(keyPath.Trim());
            if (!File.Exists(p))
                throw new AuthError($"KALSHI_PRIVATE_KEY_PATH not found: {p}");
            pem = File.ReadAllText(p, Encoding.UTF8);
        }
        else if (!string.IsNullOrWhiteSpace(keyPem))
        {
            pem = keyPem!.Replace("\\n", "\n");
        }
        else
        {
            throw new AuthError("Set KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_PEM");
        }

        return new KalshiAuth(apiKeyId, pem);
    }
}
