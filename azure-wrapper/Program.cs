using System.Net.Http.Headers;
using System.Security.Claims;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authentication.Cookies;
using Microsoft.AspNetCore.Authentication.OpenIdConnect;
using Microsoft.AspNetCore.Authorization;
using KalshiBotWrapper.Bot;
using KalshiBotWrapper.Configuration;
using KalshiBotWrapper.Dashboard;
using KalshiBotWrapper.Data;
using KalshiBotWrapper.Kalshi;
using KalshiBotWrapper.Services;
using Microsoft.EntityFrameworkCore;
using Microsoft.ApplicationInsights.Extensibility;

// ── Host / web application setup ─────────────────────────────────────────────

var builder = WebApplication.CreateBuilder(args);
builder.Configuration.AddEnvironmentVariables();
builder.Services.Configure<BotConfiguration>(
    builder.Configuration.GetSection(BotConfiguration.Section));

// ── Application Insights ──────────────────────────────────────────────────────

var cmd = builder.Configuration[$"{BotConfiguration.Section}:Command"] ?? "run";

builder.Services.AddApplicationInsightsTelemetry(o =>
{
    o.ConnectionString =
        builder.Configuration["ApplicationInsights:ConnectionString"]
        ?? builder.Configuration["APPLICATIONINSIGHTS_CONNECTION_STRING"];
});
builder.Services.Configure<TelemetryConfiguration>(config =>
{
    config.TelemetryInitializers.Add(new BotTelemetryInitializer(cmd));
});

// ── Logging ───────────────────────────────────────────────────────────────────

builder.Logging.ClearProviders();
builder.Logging.AddConsole();
builder.Logging.AddApplicationInsights();

// ── Private key file injection ────────────────────────────────────────────────

var pemFileName = builder.Configuration[$"{BotConfiguration.Section}:PrivateKeyFileName"]
    ?? "kalshi_private_key.pem";
if (!string.IsNullOrWhiteSpace(pemFileName))
{
    var pemPath = Path.Combine(AppContext.BaseDirectory, pemFileName);
    if (File.Exists(pemPath) &&
        string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("KALSHI_PRIVATE_KEY_PATH")))
    {
        Environment.SetEnvironmentVariable("KALSHI_PRIVATE_KEY_PATH", pemPath);
        Console.WriteLine($"[startup] Using packaged private key: {pemPath}");
    }
}

// ── Authentication (optional — activate by setting provider env vars) ─────────
// Microsoft Entra ID: set MICROSOFT_CLIENT_ID + MICROSOFT_CLIENT_SECRET
//   optionally MICROSOFT_TENANT_ID (default "common" = any MS account)
// GitHub OAuth:       set GITHUB_CLIENT_ID + GITHUB_CLIENT_SECRET
// Both can be active simultaneously. If neither is set, auth is disabled.

var msClientId     = Environment.GetEnvironmentVariable("MICROSOFT_CLIENT_ID");
var msClientSecret = Environment.GetEnvironmentVariable("MICROSOFT_CLIENT_SECRET");
var msTenantId     = Environment.GetEnvironmentVariable("MICROSOFT_TENANT_ID") ?? "common";
var ghClientId     = Environment.GetEnvironmentVariable("GITHUB_CLIENT_ID");
var ghClientSecret = Environment.GetEnvironmentVariable("GITHUB_CLIENT_SECRET");
var authEnabled    = !string.IsNullOrWhiteSpace(msClientId)
                  || !string.IsNullOrWhiteSpace(ghClientId);

// Comma-separated GitHub username whitelist. Empty = allow any authenticated account.
// e.g. GITHUB_ALLOWED_USERS=alice,bob
// Comma-separated email whitelist for Microsoft login. Empty = allow any authenticated account.
// e.g. AUTH_ALLOWED_EMAILS=alice@example.com,bob@example.com
var allowedGitHubUsers = (Environment.GetEnvironmentVariable("GITHUB_ALLOWED_USERS") ?? "")
    .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
    .Select(u => u.ToLowerInvariant())
    .ToHashSet();

var allowedEmails = (Environment.GetEnvironmentVariable("AUTH_ALLOWED_EMAILS") ?? "")
    .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
    .Select(e => e.ToLowerInvariant())
    .ToHashSet();

static bool IsEmailAllowed(string? email, HashSet<string> allowed) =>
    allowed.Count == 0
    || (!string.IsNullOrWhiteSpace(email) && allowed.Contains(email.ToLowerInvariant()));

static bool IsGitHubUserAllowed(string? login, HashSet<string> allowed) =>
    allowed.Count == 0
    || (!string.IsNullOrWhiteSpace(login) && allowed.Contains(login.ToLowerInvariant()));

if (authEnabled)
{
    var authBuilder = builder.Services
        .AddAuthentication(CookieAuthenticationDefaults.AuthenticationScheme)
        .AddCookie(CookieAuthenticationDefaults.AuthenticationScheme, o =>
        {
            o.LoginPath           = "/login";
            o.AccessDeniedPath    = "/login?error=access_denied";
            o.ExpireTimeSpan      = TimeSpan.FromDays(7);
            o.SlidingExpiration   = true;
            o.Cookie.HttpOnly     = true;
            o.Cookie.SecurePolicy = CookieSecurePolicy.SameAsRequest;
        });

    if (!string.IsNullOrWhiteSpace(msClientId))
    {
        authBuilder.AddOpenIdConnect("Microsoft", "Microsoft", o =>
        {
            o.Authority                          = $"https://login.microsoftonline.com/{msTenantId}/v2.0";
            o.ClientId                           = msClientId;
            o.ClientSecret                       = msClientSecret;
            o.ResponseType                       = "code";
            o.CallbackPath                       = "/signin-oidc";
            o.SaveTokens                         = false;
            o.GetClaimsFromUserInfoEndpoint      = true;
            o.MapInboundClaims                   = false;
            o.TokenValidationParameters.NameClaimType = "name";
            o.Scope.Clear();
            o.Scope.Add("openid");
            o.Scope.Add("profile");
            o.Scope.Add("email");
            o.Events.OnTicketReceived = ctx =>
            {
                // MapInboundClaims=false → Microsoft uses the raw OIDC claim name "email"
                var email = ctx.Principal?.FindFirst("email")?.Value
                         ?? ctx.Principal?.FindFirst(ClaimTypes.Email)?.Value;
                if (!IsEmailAllowed(email, allowedEmails))
                {
                    ctx.Response.Redirect("/login?error=access_denied");
                    ctx.HandleResponse();
                }
                return Task.CompletedTask;
            };
        });
    }

    if (!string.IsNullOrWhiteSpace(ghClientId))
    {
        authBuilder.AddOAuth("GitHub", "GitHub", o =>
        {
            o.ClientId                = ghClientId;
            o.ClientSecret            = ghClientSecret!;
            o.CallbackPath            = "/signin-github";
            o.AuthorizationEndpoint   = "https://github.com/login/oauth/authorize";
            o.TokenEndpoint           = "https://github.com/login/oauth/access_token";
            o.UserInformationEndpoint = "https://api.github.com/user";
            o.Scope.Add("read:user");
            o.Scope.Add("user:email");
            // Map id → NameIdentifier; full "name" → Name; fallback to "login" in the event
            o.ClaimActions.MapJsonKey(ClaimTypes.NameIdentifier, "id");
            o.ClaimActions.MapJsonKey(ClaimTypes.Name,           "name");
            o.ClaimActions.MapJsonKey(ClaimTypes.Email,          "email");
            o.Events.OnCreatingTicket = async ctx =>
            {
                using var req = new HttpRequestMessage(HttpMethod.Get, ctx.Options.UserInformationEndpoint);
                req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", ctx.AccessToken);
                req.Headers.UserAgent.ParseAdd("KalshiBot/1.0");
                using var resp = await ctx.Backchannel.SendAsync(req, ctx.HttpContext.RequestAborted);
                resp.EnsureSuccessStatusCode();
                using var doc = await JsonDocument.ParseAsync(await resp.Content.ReadAsStreamAsync());
                ctx.RunClaimActions(doc.RootElement);
                // Fall back to login handle when the user has no display name set
                string? loginName = doc.RootElement.TryGetProperty("login", out var loginProp)
                    ? loginProp.GetString() : null;
                if (ctx.Identity!.FindFirst(ClaimTypes.Name) is null && loginName is not null)
                    ctx.Identity.AddClaim(new Claim(ClaimTypes.Name, loginName));
                // Whitelist check by GitHub username — deny before the cookie is issued
                if (!IsGitHubUserAllowed(loginName, allowedGitHubUsers))
                    ctx.Fail("GitHub username not in allowed list");
            };
            // OnRemoteFailure handles ctx.Fail() from OnCreatingTicket and any other OAuth error
            o.Events.OnRemoteFailure = ctx =>
            {
                ctx.Response.Redirect("/login?error=access_denied");
                ctx.HandleResponse();
                return Task.CompletedTask;
            };
        });
    }

    // Require authentication on every route by default; public routes opt out with AllowAnonymous
    builder.Services.AddAuthorizationBuilder()
        .SetFallbackPolicy(new AuthorizationPolicyBuilder()
            .RequireAuthenticatedUser()
            .Build());
}

// ── Forwarded headers (required behind Azure Container Apps HTTPS proxy) ─────
// Azure terminates TLS and forwards X-Forwarded-Proto/Host. Without this the
// OAuth middleware builds redirect_uri as http://localhost which GitHub rejects.
builder.Services.Configure<ForwardedHeadersOptions>(o =>
{
    o.ForwardedHeaders = Microsoft.AspNetCore.HttpOverrides.ForwardedHeaders.XForwardedFor
                       | Microsoft.AspNetCore.HttpOverrides.ForwardedHeaders.XForwardedProto
                       | Microsoft.AspNetCore.HttpOverrides.ForwardedHeaders.XForwardedHost;
    // Azure proxy IPs aren't known ahead of time — trust all proxies
    o.KnownNetworks.Clear();
    o.KnownProxies.Clear();
});

// ── Azure SQL (optional — set SQL_CONNECTION_STRING to enable) ────────────────

var sqlConnectionString = builder.Configuration["SQL_CONNECTION_STRING"]
    ?? Environment.GetEnvironmentVariable("SQL_CONNECTION_STRING");
var sqlEnabled = !string.IsNullOrWhiteSpace(sqlConnectionString);

if (sqlEnabled)
{
    builder.Services.AddDbContextFactory<BotDbContext>(o =>
        o.UseSqlServer(sqlConnectionString, sql =>
        {
            sql.EnableRetryOnFailure(maxRetryCount: 3, maxRetryDelay: TimeSpan.FromSeconds(5), errorNumbersToAdd: null);
            sql.CommandTimeout(30);
        }));
    builder.Services.AddSingleton<BotRepository>();
}

// ── Services ──────────────────────────────────────────────────────────────────

builder.Services.AddSingleton<DashboardStore>(sp =>
    sqlEnabled
        ? new DashboardStore(sp.GetService<BotRepository>())
        : new DashboardStore());
builder.Services.AddHostedService<BotRunnerService>();

// ── Build app ─────────────────────────────────────────────────────────────────

var app = builder.Build();

// ── SQL schema init (no-op when SQL_CONNECTION_STRING is not set) ─────────────

if (sqlEnabled)
{
    var repo  = app.Services.GetRequiredService<BotRepository>();
    await repo.EnsureSchemaAsync();
    var saved = await repo.LoadControlsAsync();
    if (saved is not null)
        app.Services.GetRequiredService<DashboardStore>().SetControls(saved);
}

// ── Middleware ────────────────────────────────────────────────────────────────

app.UseForwardedHeaders(); // must be first — fixes redirect_uri behind Azure proxy

// Fallback: explicitly set scheme from X-Forwarded-Proto when the middleware
// above doesn't apply it (e.g. ASPNETCORE_FORWARDEDHEADERS_ENABLED not set).
app.Use((ctx, next) =>
{
    if (ctx.Request.Headers.TryGetValue("X-Forwarded-Proto", out var proto)
        && !string.IsNullOrWhiteSpace(proto))
        ctx.Request.Scheme = proto.ToString().Split(',')[0].Trim();
    return next();
});

if (authEnabled)
{
    app.UseAuthentication();
    app.UseAuthorization();
}

// ── Routes ────────────────────────────────────────────────────────────────────

app.MapGet("/health", () => Results.Ok(new { status = "healthy" })).AllowAnonymous();

// ── Auth routes ───────────────────────────────────────────────────────────────

// Login page — shows Microsoft / GitHub sign-in buttons
app.MapGet("/login", (HttpContext ctx) =>
{
    // Already logged in? Send home.
    if (authEnabled && (ctx.User.Identity?.IsAuthenticated ?? false))
        return Results.Redirect("/");
    return Results.Content(
        LoginResources.GenerateHtml(!string.IsNullOrWhiteSpace(msClientId),
                                    !string.IsNullOrWhiteSpace(ghClientId)),
        "text/html");
}).AllowAnonymous();

// Initiates an OAuth/OIDC challenge; the middleware handles the redirect
app.MapGet("/challenge", async (string? provider, string? returnUrl, HttpContext ctx) =>
{
    var scheme = provider switch
    {
        "GitHub"    => "GitHub",
        "Microsoft" => "Microsoft",
        _           => !string.IsNullOrWhiteSpace(msClientId) ? "Microsoft" : "GitHub",
    };
    var props = new AuthenticationProperties { RedirectUri = returnUrl ?? "/" };
    await ctx.ChallengeAsync(scheme, props);
}).AllowAnonymous();

// Logout — clears the session cookie and redirects to login
app.MapGet("/logout", async (HttpContext ctx) =>
{
    if (authEnabled)
        await ctx.SignOutAsync(CookieAuthenticationDefaults.AuthenticationScheme);
    return Results.Redirect("/login");
}).AllowAnonymous();

// Returns the current user's display name/email for the dashboard badge
app.MapGet("/api/me", (HttpContext ctx) =>
{
    if (!(ctx.User.Identity?.IsAuthenticated ?? false))
        return Results.Json(new { authenticated = false, name = (string?)null, email = (string?)null });
    var name  = ctx.User.Identity.Name
             ?? ctx.User.FindFirst("name")?.Value
             ?? ctx.User.FindFirst(ClaimTypes.Email)?.Value
             ?? "User";
    var email = ctx.User.FindFirst(ClaimTypes.Email)?.Value;
    return Results.Json(new { authenticated = true, name, email });
}).AllowAnonymous();

app.MapGet("/api/events", (DashboardStore store) =>
    Results.Json(store.GetEvents()));

app.MapGet("/api/series", async (DashboardStore store, BotRepository? repo, CancellationToken ct) =>
{
    if (repo is not null)
        return Results.Json(await repo.GetSeriesAsync(ct: ct));
    return Results.Json(store.GetSeries());
});

app.MapGet("/api/opportunities", (DashboardStore store) =>
    Results.Json(store.GetOpportunities()));

// GET current runtime controls
app.MapGet("/api/controls", (DashboardStore store) =>
    Results.Json(store.GetControls()));

// POST to update runtime controls — partial updates supported (null = keep current)
app.MapPost("/api/controls", async (HttpRequest req, DashboardStore store) =>
{
    BotControlsDto? body;
    try { body = await req.ReadFromJsonAsync<BotControlsDto>(); }
    catch { return Results.BadRequest("Invalid JSON"); }
    if (body is null) return Results.BadRequest("Empty body");

    var cur = store.GetControls();
    var updated = new BotControls
    {
        ExecuteEnabled       = body.ExecuteEnabled       ?? cur.ExecuteEnabled,
        ScanIntervalSeconds  = body.ScanIntervalSeconds  ?? cur.ScanIntervalSeconds,
        MaxBetsPerHour       = body.MaxBetsPerHour       ?? cur.MaxBetsPerHour,
        SpendPerBetCents     = body.SpendPerBetCents     ?? cur.SpendPerBetCents,
        MaxHoursToClose      = body.MaxHoursToClose      ?? cur.MaxHoursToClose,
        NearFiftyMarginCents  = body.NearFiftyMarginCents  ?? cur.NearFiftyMarginCents,
        MinPayoutMarginCents  = body.MinPayoutMarginCents  ?? cur.MinPayoutMarginCents,
        MaxOpenPositions      = body.MaxOpenPositions      ?? cur.MaxOpenPositions,
    };
    store.SetControls(updated);
    return Results.Json(updated);
});

// GET scan timing status
app.MapGet("/api/status", (DashboardStore store) =>
{
    var (lastScan, nextScan, total, matched, skipReason) = store.GetScanTiming();
    var (balanceCents, spentCents) = store.GetPortfolioSummary();
    var executeEnabled = store.GetControls().ExecuteEnabled;
    var now = DateTimeOffset.UtcNow;
    var secondsUntilScan = nextScan > now ? (nextScan - now).TotalSeconds : 0;
    return Results.Json(new
    {
        lastScanAt       = lastScan == DateTimeOffset.MinValue ? null : lastScan.ToString("o"),
        nextScanAt       = nextScan == DateTimeOffset.MinValue ? null : nextScan.ToString("o"),
        secondsUntilScan = Math.Round(secondsUntilScan, 1),
        serverTimeUtc    = now.ToString("o"),
        lastScanTotal    = total,
        lastScanMatched  = matched,
        skipReason,
        balanceCents,
        spentCents,
        executeEnabled,
    });
});

// POST to force an immediate scan
app.MapPost("/api/scan/force", (DashboardStore store) =>
{
    var triggered = store.TriggerForceScan();
    return Results.Json(new { triggered, message = triggered ? "Scan triggered" : "Scan already pending" });
});

// GET suggestion history with projected P&L
app.MapGet("/api/suggestions", async (DashboardStore store, BotRepository? repo, CancellationToken ct) =>
{
    if (repo is not null)
        return Results.Json(await repo.GetSuggestionsAsync(ct));

    // In-memory fallback (no SQL configured)
    var suggestions = store.GetSuggestions();
    int cumulative = 0;
    var rows = suggestions.OrderBy(s => s.SuggestedAt).Select(s =>
    {
        if (s.Resolution != null) cumulative += s.OutcomeCents ?? 0;
        return new
        {
            id               = s.Id,
            ticker           = s.Ticker,
            eventTicker      = s.EventTicker,
            title            = s.Title,
            yesAskCents      = s.YesAskCents,
            midCents         = s.MidCents,
            contractCount    = s.ContractCount,
            spendCents       = s.SpendCents,
            suggestedAt      = s.SuggestedAt.ToString("o"),
            suggestedAtUnix  = s.SuggestedAt.ToUnixTimeMilliseconds() / 1000.0,
            closeTime        = s.CloseTime.ToString("o"),
            scanRank         = s.ScanRank,
            executed         = s.Executed,
            executedAt       = s.ExecutedAt?.ToString("o"),
            resolution       = s.Resolution,
            outcomeCents     = s.OutcomeCents,
            cumulativeCents  = cumulative,
            url              = s.Url,
            executeError     = s.ExecuteError,
        };
    }).ToList();
    return Results.Json(rows);
});

// POST to manually execute a single order from the dashboard
app.MapPost("/api/execute", async (HttpRequest req, DashboardStore store, CancellationToken ct) =>
{
    ExecuteOrderDto? body;
    try { body = await req.ReadFromJsonAsync<ExecuteOrderDto>(); }
    catch { return Results.BadRequest(new { error = "Invalid JSON" }); }
    if (body is null || string.IsNullOrWhiteSpace(body.Ticker))
        return Results.BadRequest(new { error = "ticker is required" });

    var settings = TradingSettings.FromEnvironment();

    if (settings.DryRun)
    {
        var action = body.Action ?? "buy";
        store.RecordEvent("dry_run_manual", new {
            ticker = body.Ticker, side = body.Side, action,
            yes_price_cents = body.YesPriceCents, count = body.Count
        });
        store.MarkSuggestionExecuted(body.Ticker);
        return Results.Json(new {
            success  = true,
            dry_run  = true,
            message  = $"[DRY RUN] Would {action}: {body.Count}x {body.Ticker} {body.Side.ToUpper()} @ {body.YesPriceCents}¢",
            order_id = "(dry-run)"
        });
    }

    try
    {
        var auth = KalshiAuthLoader.Build(
            settings.KalshiApiKeyId, settings.KalshiPrivateKeyPath, settings.KalshiPrivateKeyPem);
        using var client = new KalshiRestClient(settings.RestBaseUrl, auth);
        var orderReq = new CreateOrderRequest
        {
            Ticker        = body.Ticker,
            ClientOrderId = Guid.NewGuid().ToString("N"),
            Side          = body.Side,
            Action        = body.Action ?? "buy",
            Count         = body.Count,
            YesPrice      = body.YesPriceCents,
            TimeInForce   = "good_till_canceled",
            Type          = "limit"
        };
        var resp = await client.CreateOrderAsync(orderReq, ct);
        var orderId = resp.Order?.OrderId ?? "(unknown)";
        store.RecordEvent("manual_order", new {
            ticker = body.Ticker, side = body.Side,
            yes_price_cents = body.YesPriceCents, count = body.Count,
            order_id = orderId
        });
        store.MarkSuggestionExecuted(body.Ticker);
        return Results.Json(new { success = true, dry_run = false,
            message = $"Order placed: {orderId}", order_id = orderId });
    }
    catch (KalshiApiException ex)
    {
        return Results.Json(new { success = false, error = ex.Message }, statusCode: ex.StatusCode);
    }
    catch (Exception ex)
    {
        return Results.Json(new { success = false, error = ex.Message }, statusCode: 500);
    }
});

// GET current open positions
app.MapGet("/api/positions", async (DashboardStore store, CancellationToken ct) =>
{
    var settings = TradingSettings.FromEnvironment();
    try
    {
        var auth = KalshiAuthLoader.Build(
            settings.KalshiApiKeyId, settings.KalshiPrivateKeyPath, settings.KalshiPrivateKeyPem);
        using var client = new KalshiRestClient(settings.RestBaseUrl, auth);
        var resp = await client.GetPositionsAsync(countFilter: "position", limit: 500, ct: ct);
        var dtos = resp.MarketPositions
            .Where(p => p.Position is not null && Math.Abs(p.Position.Value) >= 0.5)
            .Select(p =>
            {
                var contracts = (int)Math.Round(Math.Abs(p.Position!.Value));
                var side      = p.Position.Value > 0 ? "yes" : "no";
                int? estEntry = null;
                if (p.TotalTraded.HasValue && contracts > 0)
                    estEntry = Math.Clamp(
                        (int)Math.Round(p.TotalTraded.Value / contracts * 100), 1, 99);
                var (closeTime, title, url) = store.GetMarketInfo(p.Ticker ?? "");
                return new PositionDto(
                    p.Ticker ?? "",
                    side,
                    contracts,
                    estEntry,
                    Math.Round(p.MarketExposure ?? 0, 4),
                    Math.Round(p.RealizedPnl    ?? 0, 4),
                    closeTime?.ToString("o"),
                    title,
                    url);
            })
            .OrderByDescending(p => p.Contracts)
            .ToList();
        return Results.Json(dtos);
    }
    catch (KalshiApiException ex)
    {
        return Results.Json(new { error = ex.Message }, statusCode: ex.StatusCode);
    }
    catch (Exception ex)
    {
        return Results.Json(new { error = ex.Message }, statusCode: 500);
    }
});

// GET raw Kalshi positions response for diagnosing why the sidebar isn't populating
app.MapGet("/api/positions/debug", async (CancellationToken ct) =>
{
    var settings = TradingSettings.FromEnvironment();
    try
    {
        var auth = KalshiAuthLoader.Build(
            settings.KalshiApiKeyId, settings.KalshiPrivateKeyPath, settings.KalshiPrivateKeyPem);
        using var client = new KalshiRestClient(settings.RestBaseUrl, auth);

        var raw     = await client.GetRawPositionsJsonAsync(500, ct);
        var parsed  = await client.GetPositionsAsync(countFilter: "position", limit: 500, ct: ct);
        var nonZero = parsed.MarketPositions.Count(p => p.Position.HasValue && Math.Abs(p.Position.Value) >= 0.5);

        return Results.Json(new
        {
            kalshiEnv      = settings.KalshiEnv,
            restBaseUrl    = settings.RestBaseUrl,
            totalParsed    = parsed.MarketPositions.Count,
            nonZeroCount   = nonZero,
            rawResponse    = System.Text.Json.JsonDocument.Parse(raw).RootElement,
            parsedPositions = parsed.MarketPositions,
        });
    }
    catch (KalshiApiException ex)
    {
        return Results.Json(new { error = ex.Message, statusCode = ex.StatusCode,
            kalshiEnv = settings.KalshiEnv, restBaseUrl = settings.RestBaseUrl }, statusCode: ex.StatusCode);
    }
    catch (Exception ex)
    {
        return Results.Json(new { error = ex.Message, exceptionType = ex.GetType().Name,
            kalshiEnv = settings.KalshiEnv, restBaseUrl = settings.RestBaseUrl }, statusCode: 500);
    }
});

app.MapGet("/", () => Results.Content(DashboardResources.Html, "text/html"));
app.MapGet("/dashboard", () => Results.Content(DashboardResources.Html, "text/html"));

await app.RunAsync();

// ── DTO for partial-update POST /api/controls ─────────────────────────────────

record PositionDto(
    [property: JsonPropertyName("ticker")]        string  Ticker,
    [property: JsonPropertyName("side")]          string  Side,
    [property: JsonPropertyName("contracts")]     int     Contracts,
    [property: JsonPropertyName("estEntryCents")] int?    EstEntryCents,
    [property: JsonPropertyName("marketExposure")]double  MarketExposure,
    [property: JsonPropertyName("realizedPnl")]   double  RealizedPnl,
    [property: JsonPropertyName("closeTime")]     string? CloseTime,
    [property: JsonPropertyName("title")]         string? Title,
    [property: JsonPropertyName("url")]           string? Url
);

record ExecuteOrderDto(
    [property: JsonPropertyName("ticker")]        string  Ticker,
    [property: JsonPropertyName("side")]          string  Side,
    [property: JsonPropertyName("action")]        string? Action,        // "buy" (default) or "sell"
    [property: JsonPropertyName("yesPriceCents")] int     YesPriceCents,
    [property: JsonPropertyName("count")]         int     Count
);

record BotControlsDto(
    [property: JsonPropertyName("executeEnabled")]       bool?   ExecuteEnabled,
    [property: JsonPropertyName("scanIntervalSeconds")]  int?    ScanIntervalSeconds,
    [property: JsonPropertyName("maxBetsPerHour")]       int?    MaxBetsPerHour,
    [property: JsonPropertyName("spendPerBetCents")]     int?    SpendPerBetCents,
    [property: JsonPropertyName("maxHoursToClose")]      double? MaxHoursToClose,
    [property: JsonPropertyName("nearFiftyMarginCents")]  int?    NearFiftyMarginCents,
    [property: JsonPropertyName("minPayoutMarginCents")]  int?    MinPayoutMarginCents,
    [property: JsonPropertyName("maxOpenPositions")]      int?    MaxOpenPositions
);

// ── Dashboard HTML ────────────────────────────────────────────────────────────

static class LoginResources
{
    public static string GenerateHtml(bool hasMicrosoft, bool hasGitHub) => $$"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Sign in — Kalshi bot</title>
  <style>
    :root { --bg:#0f1419; --card:#1a2332; --border:#2a3544; --text:#e7ecf3; --muted:#8b9aab; }
    * { box-sizing: border-box; }
    body { font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg);
           color: var(--text); margin: 0; min-height: 100vh;
           display: flex; align-items: center; justify-content: center; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
            padding: 2rem 2.5rem; width: 100%; max-width: 360px; text-align: center; }
    h1 { font-size: 1.15rem; font-weight: 600; margin: 0 0 0.35rem; }
    p  { color: var(--muted); font-size: 0.875rem; margin: 0 0 1.75rem; }
    .btn { display: flex; align-items: center; justify-content: center; gap: 0.65rem;
           width: 100%; padding: 0.65rem 1rem; border-radius: 7px; font-size: 0.9rem;
           font-weight: 500; cursor: pointer; border: 1px solid var(--border);
           text-decoration: none; color: var(--text); background: #1d2d40;
           margin-bottom: 0.75rem; transition: background 0.15s; }
    .btn:hover { background: #243548; }
    .btn svg { flex-shrink: 0; }
    .divider { display: none; }
    .error-box { background: rgba(242,81,81,0.12); border: 1px solid rgba(242,81,81,0.3);
                 border-radius: 6px; color: #f25151; font-size: 0.8rem;
                 padding: 0.6rem 0.85rem; margin-bottom: 1.25rem; text-align: left; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Kalshi bot monitor</h1>
    <p>Sign in to access the dashboard.</p>
    <div id="err-box" class="error-box" style="display:none"></div>
    {{(hasMicrosoft ? """
    <a class="btn" href="/challenge?provider=Microsoft&returnUrl=/">
      <svg width="20" height="20" viewBox="0 0 21 21" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="1" y="1" width="9" height="9" fill="#f25022"/>
        <rect x="11" y="1" width="9" height="9" fill="#7fba00"/>
        <rect x="1" y="11" width="9" height="9" fill="#00a4ef"/>
        <rect x="11" y="11" width="9" height="9" fill="#ffb900"/>
      </svg>
      Sign in with Microsoft
    </a>
    """ : "")}}
    {{(hasGitHub ? """
    <a class="btn" href="/challenge?provider=GitHub&returnUrl=/">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57
          0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695
          -.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305
          3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925
          0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23
          .96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23
          .66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225
          0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22
          0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12
          c0-6.63-5.37-12-12-12z"/>
      </svg>
      Sign in with GitHub
    </a>
    """ : "")}}
  </div>
  <script>
    const p = new URLSearchParams(location.search);
    const err = p.get('error');
    if (err) {
      const box = document.getElementById('err-box');
      box.style.display = '';
      box.textContent = err === 'access_denied'
        ? 'Access denied — your account is not on the allowed list.'
        : 'Sign-in error: ' + err;
    }
  </script>
</body>
</html>
""";
}

static class DashboardResources
{
    public const string Html = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Kalshi bot monitor</title>
  <style>
    :root {
      --bg:#0f1419; --card:#1a2332; --border:#2a3544;
      --text:#e7ecf3; --muted:#8b9aab;
      --ok:#3ecf8e; --warn:#f5a623; --err:#f25151; --blue:#6eb5ff;
    }
    * { box-sizing: border-box; }
    body { font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--text); margin:0; padding:1rem 1.25rem 2rem; }
    h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.25rem; }
    h2 { font-size: 0.9375rem; font-weight: 600; margin: 0 0 0.75rem; color: var(--muted); }
    p.sub { color: var(--muted); font-size: 0.875rem; margin: 0 0 1.25rem; }
    .page-header { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 0; }
    .page-header-text { flex: 1; }
    .page-header-actions { display: flex; align-items: center; gap: 0.5rem; padding-top: 0.15rem; }
    .btn-signout { background: none; border: 1px solid var(--border); color: var(--muted);
      border-radius: 5px; cursor: pointer; font-size: 0.78rem; padding: 0.3rem 0.65rem;
      white-space: nowrap; }
    .btn-signout:hover { color: var(--text); border-color: var(--muted); }
    #header-user-name { font-size: 0.78rem; color: var(--muted); max-width: 140px;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .btn-hamburger { background: none; border: none; color: var(--muted); cursor: pointer;
      font-size: 1.25rem; line-height: 1; padding: 0; display: flex; align-items: center; flex-shrink: 0; }
    .btn-hamburger:hover { color: var(--text); }

    .card { background: var(--card); border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1rem; }

    /* Skip-reason notice */
    .skip-reason-bar {
      display: flex; align-items: center; gap: 0.5rem;
      background: rgba(245,166,35,0.08); border: 1px solid rgba(245,166,35,0.25);
      border-radius: 6px; padding: 0.45rem 1rem; margin-bottom: 1rem;
      font-size: 0.8125rem; color: var(--warn);
    }
    .skip-reason-icon { font-size: 1rem; flex-shrink: 0; }

    /* Scan status bar */
    .scan-bar {
      display: flex; align-items: center; gap: 1.25rem; flex-wrap: wrap;
      background: var(--card); border-radius: 8px; padding: 0.75rem 1.25rem;
      margin-bottom: 1rem; border: 1px solid var(--border);
    }
    .scan-item { display: flex; flex-direction: column; gap: 0.15rem; }
    .scan-item .label { font-size: 0.7rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
    .scan-item .value { font-size: 0.9rem; font-weight: 600; font-variant-numeric: tabular-nums; }
    .countdown-ring { position: relative; width: 48px; height: 48px; flex-shrink: 0; }
    .countdown-ring svg { transform: rotate(-90deg); }
    .countdown-ring .bg { stroke: var(--border); fill: none; stroke-width: 4; }
    .countdown-ring .fg { stroke: var(--blue); fill: none; stroke-width: 4; stroke-linecap: round;
      stroke-dasharray: 126; stroke-dashoffset: 126; transition: stroke-dashoffset 0.9s linear; }
    .countdown-ring .num {
      position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
      font-size: 0.7rem; font-weight: 700; color: var(--blue); font-variant-numeric: tabular-nums;
    }
    .btn-force, .btn-settings {
      background: #1d4ed8; color: #fff; border: none; border-radius: 5px;
      padding: 0.45rem 1rem; font-size: 0.8125rem; cursor: pointer; font-weight: 500;
      display: flex; align-items: center; gap: 0.4rem; white-space: nowrap;
    }
    .btn-force { margin-left: auto; }
    .btn-settings { background: #374151; }
    .btn-force:hover { background: #1e40af; }
    .btn-settings:hover { background: #4b5563; }
    .btn-force:disabled { background: #2a3544; color: var(--muted); cursor: not-allowed; }
    .btn-force.scanning { background: #0e7490; }
    .execute-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); flex-shrink: 0;
      box-shadow: 0 0 0 0 rgba(62,207,142,0.6); animation: pulse-dot 1.8s ease-in-out infinite; }
    @keyframes pulse-dot {
      0%,100% { box-shadow: 0 0 0 0 rgba(62,207,142,0.5); }
      50%      { box-shadow: 0 0 0 5px rgba(62,207,142,0); }
    }
    #execute-indicator { display: none; align-items: center; gap: 0.4rem;
      font-size: 0.75rem; font-weight: 600; color: var(--ok); white-space: nowrap; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .spinner { display: inline-block; width: 12px; height: 12px; border: 2px solid rgba(255,255,255,0.3);
      border-top-color: #fff; border-radius: 50%; animation: spin 0.7s linear infinite; }

    /* Modals */
    dialog { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
      color: var(--text); padding: 0; min-width: 340px; max-width: 600px; width: 100%; }
    dialog::backdrop { background: rgba(0,0,0,0.6); }
    .modal-head { display: flex; align-items: center; justify-content: space-between;
      padding: 0.9rem 1.25rem 0.75rem; border-bottom: 1px solid var(--border); }
    .modal-head h3 { margin: 0; font-size: 1rem; font-weight: 600; }
    .modal-close { background: none; border: none; color: var(--muted); font-size: 1.2rem;
      cursor: pointer; padding: 0 0.25rem; line-height: 1; }
    .modal-close:hover { color: var(--text); }
    .modal-body { padding: 1rem 1.25rem; }
    .modal-foot { display: flex; justify-content: flex-end; gap: 0.6rem;
      padding: 0.75rem 1.25rem; border-top: 1px solid var(--border); }
    .btn-cancel { background: #374151; color: #fff; border: none; border-radius: 5px;
      padding: 0.4rem 1rem; cursor: pointer; font-size: 0.8125rem; }
    .btn-cancel:hover { background: #4b5563; }
    .btn-confirm { background: #16a34a; color: #fff; border: none; border-radius: 5px;
      padding: 0.4rem 1rem; cursor: pointer; font-size: 0.8125rem; font-weight: 600; }
    .btn-confirm:hover { background: #15803d; }
    .btn-confirm:disabled { background: #2a3544; color: var(--muted); cursor: not-allowed; }
    .form-row { display: flex; flex-direction: column; gap: 0.3rem; margin-bottom: 0.75rem; }
    .form-row label { font-size: 0.75rem; color: var(--muted); }
    .form-row input[type=number], .form-row input[type=text] {
      background: #0f1419; border: 1px solid var(--border); border-radius: 4px;
      color: var(--text); padding: 0.4rem 0.6rem; font-size: 0.875rem; width: 100%; }
    .form-row .readonly { opacity: 0.6; }
    .side-toggle { display: flex; gap: 0.5rem; }
    .side-toggle label { display: flex; align-items: center; gap: 0.35rem; cursor: pointer;
      font-size: 0.875rem; padding: 0.3rem 0.75rem; border-radius: 4px;
      border: 1px solid var(--border); }
    .side-toggle input[type=radio] { display: none; }
    .side-toggle label:has(input:checked) { border-color: var(--ok); color: var(--ok); background: rgba(62,207,142,0.08); }
    .modal-result { font-size: 0.8125rem; margin-top: 0.5rem; padding: 0.4rem 0.6rem;
      border-radius: 4px; display: none; }
    .modal-result.ok  { background: rgba(62,207,142,0.12); color: var(--ok); display: block; }
    .modal-result.err { background: rgba(242,81,81,0.12);  color: var(--err); display: block; }

    /* Execute button in table */
    .btn-exec { background: transparent; border: 1px solid var(--border); color: var(--muted);
      border-radius: 4px; padding: 0.15rem 0.5rem; font-size: 0.7rem; cursor: pointer; white-space: nowrap; }
    .btn-exec:hover { border-color: var(--ok); color: var(--ok); }

    /* Top-pick row highlight */
    .top-pick { border-left: 3px solid #f5a623 !important; background: rgba(245,166,35,0.06) !important; }
    .top-pick td:first-child { padding-left: calc(0.75rem - 3px); }
    .badge-top { background: #f5a623; color: #0f1419; font-size: 0.6rem; font-weight: 700;
      border-radius: 3px; padding: 0.1rem 0.3rem; vertical-align: middle; margin-left: 0.35rem;
      letter-spacing: 0.03em; }
    .btn-exec-primary { background: #f5a623 !important; border-color: #f5a623 !important;
      color: #0f1419 !important; font-weight: 600; }
    .btn-exec-primary:hover { background: #e09310 !important; border-color: #e09310 !important; }

    /* Chart tabs */
    .chart-tabs { display: flex; gap: 0.5rem; margin-bottom: 0.75rem; }
    .chart-tab { background: none; border: 1px solid var(--border); color: var(--muted);
      border-radius: 5px; padding: 0.3rem 0.85rem; font-size: 0.8125rem; cursor: pointer; }
    .chart-tab.active { background: #1d4ed8; border-color: #1d4ed8; color: #fff; font-weight: 500; }
    .chart-tab:not(.active):hover { border-color: var(--muted); color: var(--text); }

    /* Suggestion history table */
    .suggestion-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 0.5rem; }
    .proj-balance { font-size: 0.85rem; font-weight: 600; }
    .proj-pos { color: var(--ok); }
    .proj-neg { color: var(--err); }
    .proj-zero { color: var(--muted); }
    .sug-won  { color: var(--ok); }
    .sug-lost { color: var(--err); }
    .sug-exec { color: var(--blue); }
    .sug-pend { color: var(--muted); }
    .sug-error { color: var(--err); cursor: help; text-decoration: underline dotted; }
    .sug-rank1 { color: #f5a623; font-weight: 700; }

    .mkt-link { color: inherit; text-decoration: none; }
    .mkt-link:hover code { color: var(--blue); text-decoration: underline; }

    /* Page layout with collapsible sidebar */
    .page-layout { display: flex; align-items: flex-start; gap: 0; }
    .sidebar {
      width: 300px; flex-shrink: 0; overflow: hidden;
      transition: width 0.25s ease;
      background: var(--card); border-radius: 8px; margin-right: 1rem;
      position: sticky; top: 1rem; max-height: calc(100vh - 2rem); display: flex; flex-direction: column;
    }
    .sidebar.collapsed { width: 0; margin-right: 0; }
    .sidebar-inner { width: 300px; display: flex; flex-direction: column; flex: 1; overflow: hidden; }
    .sidebar-header { display: flex; align-items: center; justify-content: space-between;
      padding: 0.75rem 1rem 0.5rem; border-bottom: 1px solid var(--border); flex-shrink: 0; }
    .sidebar-header h2 { margin: 0; font-size: 0.9rem; font-weight: 600; color: var(--text); }
    .sidebar-close { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 1rem; padding: 0; }
    .sidebar-close:hover { color: var(--text); }
    .sidebar-body { overflow-y: auto; flex: 1; padding: 0.5rem 0; }
    .pos-row { padding: 0.55rem 1rem; border-bottom: 1px solid var(--border); }
    .pos-row:last-child { border-bottom: none; }
    .pos-ticker { font-size: 0.78rem; color: var(--text); display: block;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      cursor: default; line-height: 1.3; }
    .pos-meta { display: flex; align-items: center; justify-content: space-between; margin-top: 0.25rem; gap: 0.4rem; }
    .pos-payout { font-size: 0.68rem; color: var(--ok); margin-top: 0.2rem; }
    .pos-time { font-size: 0.68rem; color: var(--muted); margin-top: 0.2rem; }
    .pos-time.urgent { color: var(--warn); font-weight: 600; }
    .pos-time.critical { color: var(--err); font-weight: 700; }
    .pos-badge { font-size: 0.7rem; padding: 0.1rem 0.35rem; border-radius: 3px; font-weight: 600; }
    .pos-badge-yes { background: rgba(62,207,142,0.15); color: var(--ok); }
    .pos-badge-no  { background: rgba(242,81,81,0.15);  color: var(--err); }
    .pos-info { font-size: 0.75rem; color: var(--muted); flex: 1; }
    .btn-sell { background: transparent; border: 1px solid var(--err); color: var(--err);
      border-radius: 4px; padding: 0.15rem 0.55rem; font-size: 0.7rem; cursor: pointer; white-space: nowrap; flex-shrink: 0; }
    .btn-sell:hover { background: rgba(242,81,81,0.1); }
    .pos-empty { padding: 1rem; color: var(--muted); font-size: 0.8125rem; text-align: center; }
    .pos-error { padding: 1rem; color: var(--err); font-size: 0.8125rem; }
    .pos-loading { padding: 1rem; color: var(--muted); font-size: 0.8125rem; font-style: italic; }
    .sidebar-refresh { display: flex; align-items: center; justify-content: space-between;
      padding: 0.5rem 1rem; border-top: 1px solid var(--border); flex-shrink: 0; }
    .pos-count { font-size: 0.75rem; color: var(--muted); }
    .btn-refresh { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 0.75rem; }
    .btn-refresh:hover { color: var(--text); }
    .main-content { flex: 1; min-width: 0; }

    /* Controls panel */
    .controls-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0.75rem; margin-bottom: 1rem; }
    .ctrl-group { display: flex; flex-direction: column; gap: 0.3rem; }
    .ctrl-group label { font-size: 0.75rem; color: var(--muted); }
    .ctrl-group input[type=number] {
      background: #0f1419; border: 1px solid var(--border); border-radius: 4px;
      color: var(--text); padding: 0.35rem 0.5rem; font-size: 0.8125rem; width: 100%;
    }
    .execute-row { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 1rem; }
    .toggle { position: relative; display: inline-block; width: 42px; height: 24px; }
    .toggle input { opacity: 0; width: 0; height: 0; }
    .slider { position: absolute; cursor: pointer; inset: 0; background: #2a3544; border-radius: 24px; transition: .2s; }
    .slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background: white; border-radius: 50%; transition: .2s; }
    input:checked + .slider { background: var(--ok); }
    input:checked + .slider:before { transform: translateX(18px); }
    .execute-label { font-size: 0.875rem; font-weight: 500; }
    .execute-warning { font-size: 0.75rem; color: var(--warn); margin-left: 0.5rem; }

    .btn { background: #3b82f6; color: #fff; border: none; border-radius: 5px; padding: 0.45rem 1.1rem; font-size: 0.8125rem; cursor: pointer; font-weight: 500; }
    .btn:hover { background: #2563eb; }
    .save-status { font-size: 0.75rem; color: var(--ok); margin-left: 0.75rem; display: none; }

    /* Tables */
    table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
    th, td { text-align: left; padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--border); vertical-align: middle; }
    th { color: var(--muted); font-weight: 500; }
    .mid-bar { display: inline-block; width: 70px; height: 7px; background: var(--border); border-radius: 4px; vertical-align: middle; position: relative; overflow: hidden; }
    .mid-fill { height: 100%; border-radius: 4px; background: var(--blue); position: absolute; }
    .badge { display: inline-block; padding: 0.1rem 0.4rem; border-radius: 3px; font-size: 0.7rem; font-family: ui-monospace, monospace; }
    .badge-ok { background: rgba(62,207,142,0.15); color: var(--ok); }
    .badge-warn { background: rgba(245,166,35,0.15); color: var(--warn); }

    /* Grouped opportunity rows */
    .group-hdr { cursor: pointer; user-select: none; background: rgba(110,181,255,0.04); }
    .group-hdr:hover td { background: rgba(110,181,255,0.09); }
    .group-hdr td { font-weight: 500; }
    .expand-btn { display: inline-block; width: 18px; text-align: center; font-size: 0.7rem; color: var(--muted); transition: transform 0.15s; }
    .expand-btn.open { transform: rotate(90deg); }
    .child-row td { padding-left: 2rem; color: var(--muted); font-size: 0.775rem; }
    .child-row:hover td { background: #141c26; }
    .child-row { display: none; }
    .child-row.visible { display: table-row; }

    /* Pagination */
    .page-bar { display: flex; align-items: center; gap: 0.6rem; margin-top: 0.75rem; font-size: 0.8125rem; color: var(--muted); }
    .page-btn { background: var(--card); border: 1px solid var(--border); color: var(--text); border-radius: 4px; padding: 0.25rem 0.65rem; cursor: pointer; font-size: 0.8125rem; }
    .page-btn:hover:not(:disabled) { border-color: var(--blue); color: var(--blue); }
    .page-btn:disabled { opacity: 0.35; cursor: not-allowed; }

    /* Events */
    .kind { font-family: ui-monospace, monospace; font-size: 0.75rem; }
    .kind-dry_run { color: var(--ok); }
    .kind-live_submit { color: var(--blue); }
    .kind-force_scan { color: var(--blue); }
    .kind-blocked { color: var(--warn); }
    .kind-refused, .kind-error, .kind-auth_401, .kind-scan_error { color: var(--err); }
    .kind-heartbeat, .kind-scan { color: var(--muted); }
    pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 0.75rem; color: #c5d0dc; }

    .chart-wrap { background: var(--card); border-radius: 8px; padding: 0.75rem 1rem; margin-bottom: 1rem; }
    .chart-wrap canvas { max-height: 200px; }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
</head>
<body>
  <div class="page-header">
    <div class="page-header-text">
      <h1>Kalshi bot — opportunity monitor</h1>
      <p class="sub">Scans for markets closing within the configured time window whose YES probability falls within the min/max range. Controls take effect on the next scan cycle.</p>
    </div>
    <div id="header-user-badge" class="page-header-actions" style="display:none;">
      <span id="header-user-name"></span>
      <button class="btn-signout" onclick="signOut()">↩ Sign out</button>
    </div>
  </div>

  <!-- ── Scan status bar ───────────────────────────────────────────────────── -->
  <div class="scan-bar">
    <button class="btn-hamburger" onclick="toggleSidebar()" title="Positions">&#9776;</button>
    <div class="countdown-ring" title="Time until next scan">
      <svg viewBox="0 0 44 44" width="48" height="48">
        <circle class="bg" cx="22" cy="22" r="20"/>
        <circle class="fg" id="ring-fg" cx="22" cy="22" r="20"/>
      </svg>
      <div class="num" id="ring-num">–</div>
    </div>

    <div class="scan-item">
      <span class="label">Next scan</span>
      <span class="value" id="next-scan-val">–</span>
    </div>
    <div class="scan-item">
      <span class="label">Last scan</span>
      <span class="value" id="last-scan-val">–</span>
    </div>
    <div class="scan-item">
      <span class="label">Markets scanned</span>
      <span class="value" id="scan-total-bar">–</span>
    </div>
    <div class="scan-item">
      <span class="label">Fit criteria</span>
      <span class="value" id="scan-matched-bar">–</span>
    </div>
    <div class="scan-item">
      <span class="label">Balance</span>
      <span class="value" id="balance-bar">–</span>
    </div>
    <div id="execute-indicator">
      <span class="execute-dot"></span>
      EXECUTE ON
    </div>
    <button class="btn-force" id="btn-force" onclick="forceScan()">
      ▶ Force Scan
    </button>
    <button class="btn-settings" onclick="openSettings()">
      ⚙ Settings
    </button>
  </div>

  <!-- ── Skip reason notice ────────────────────────────────────────────────── -->
  <div id="skip-reason-bar" class="skip-reason-bar" style="display:none">
    <span class="skip-reason-icon">ℹ</span>
    <span id="skip-reason-text"></span>
  </div>

  <!-- ── Settings modal ───────────────────────────────────────────────────── -->
  <dialog id="dlg-settings">
    <div class="modal-head">
      <h3>Runtime Controls</h3>
      <button class="modal-close" onclick="document.getElementById('dlg-settings').close()">✕</button>
    </div>
    <div class="modal-body">
      <div class="execute-row" style="margin-bottom:1rem">
        <label class="toggle">
          <input type="checkbox" id="ctrl-execute" onchange="markDirty()"/>
          <span class="slider"></span>
        </label>
        <span class="execute-label">Execute trades</span>
        <span class="execute-warning" id="exec-warning" style="display:none">⚠ LIVE orders will be placed</span>
      </div>
      <div class="controls-grid">
        <div class="ctrl-group">
          <label>Scan interval (seconds)</label>
          <input type="number" id="ctrl-interval" min="10" max="3600" oninput="markDirty()"/>
        </div>
        <div class="ctrl-group">
          <label>Max bets per hour</label>
          <input type="number" id="ctrl-maxbets" min="0" max="100" oninput="markDirty()"/>
        </div>
        <div class="ctrl-group">
          <label>Spend per bet ($)</label>
          <input type="number" id="ctrl-spend" min="0.01" step="0.01" oninput="markDirty()" placeholder="e.g. 5.00"/>
        </div>
        <div class="ctrl-group">
          <label>Max time remaining (hours)</label>
          <input type="number" id="ctrl-maxhours" min="0.5" max="720" step="0.5" oninput="markDirty()"/>
        </div>
        <div class="ctrl-group">
          <label>Max cents below 50¢ (upper bound, 1–49)</label>
          <input type="number" id="ctrl-margin" min="1" max="49" oninput="markDirty()"/>
        </div>
        <div class="ctrl-group">
          <label>Min payout margin (ask must be ≥N¢ below 50¢, 0 = off)</label>
          <input type="number" id="ctrl-minpayout" min="0" max="49" oninput="markDirty()"/>
        </div>
        <div class="ctrl-group">
          <label>Max open positions (0 = unlimited)</label>
          <input type="number" id="ctrl-maxpos" min="0" max="1000" oninput="markDirty()"/>
        </div>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn-cancel" onclick="document.getElementById('dlg-settings').close()">Cancel</button>
      <button class="btn-confirm" onclick="saveControls()">Apply</button>
      <span class="save-status" id="save-status" style="margin:0;align-self:center">✓ Saved</span>
    </div>
  </dialog>

  <!-- ── Execute dialog ───────────────────────────────────────────────────── -->
  <dialog id="dlg-execute">
    <div class="modal-head">
      <h3>Place Order</h3>
      <button class="modal-close" onclick="closeExecute()">✕</button>
    </div>
    <div class="modal-body">
      <div class="form-row">
        <label>Ticker</label>
        <input type="text" id="exec-ticker" class="readonly" readonly/>
      </div>
      <div class="form-row">
        <label>Title</label>
        <a id="exec-market-link" href="#" target="_blank" rel="noopener noreferrer"
           style="font-size:0.8rem;color:var(--blue);text-decoration:none;line-height:1.4;word-break:break-all;display:block;">
          <span id="exec-title"></span>
        </a>
      </div>
      <div class="form-row">
        <label>Side</label>
        <div class="side-toggle">
          <label><input type="radio" name="exec-side" value="yes" checked> YES</label>
          <label><input type="radio" name="exec-side" value="no"> NO</label>
        </div>
      </div>
      <div class="form-row">
        <label>Limit price (¢)</label>
        <input type="number" id="exec-price" min="1" max="99"/>
      </div>
      <div class="form-row">
        <label>Contracts</label>
        <input type="number" id="exec-count" min="1" max="9999"/>
      </div>
      <div class="modal-result" id="exec-result"></div>
    </div>
    <div class="modal-foot">
      <button class="btn-cancel" onclick="closeExecute()">Cancel</button>
      <button class="btn-confirm" id="exec-confirm" onclick="submitExecute()">Place Order</button>
    </div>
  </dialog>

  <!-- ── Sell dialog ──────────────────────────────────────────────────────── -->
  <dialog id="dlg-sell">
    <div class="modal-head">
      <h3>Sell Position</h3>
      <button class="modal-close" onclick="closeSell()">✕</button>
    </div>
    <div class="modal-body">
      <div class="form-row">
        <label>Ticker</label>
        <input type="text" id="sell-ticker" class="readonly" readonly/>
      </div>
      <div class="form-row">
        <label>Side</label>
        <input type="text" id="sell-side-display" class="readonly" readonly/>
      </div>
      <div class="form-row">
        <label>Held contracts</label>
        <input type="text" id="sell-held" class="readonly" readonly/>
      </div>
      <div class="form-row">
        <label>Contracts to sell</label>
        <input type="number" id="sell-count" min="1"/>
      </div>
      <div class="form-row">
        <label>Limit price (¢) — minimum you'll accept</label>
        <input type="number" id="sell-price" min="1" max="99"/>
      </div>
      <div class="modal-result" id="sell-result"></div>
    </div>
    <div class="modal-foot">
      <button class="btn-cancel" onclick="closeSell()">Cancel</button>
      <button class="btn-confirm" id="sell-confirm" onclick="submitSell()" style="background:#dc2626">Sell</button>
    </div>
  </dialog>

  <!-- ── Page layout (sidebar + main) ─────────────────────────────────────── -->
  <div class="page-layout">

  <!-- ── Left sidebar: Positions ──────────────────────────────────────────── -->
  <aside id="sidebar" class="sidebar collapsed">
    <div class="sidebar-inner">
      <div class="sidebar-header">
        <h2>Open Positions</h2>
        <button class="sidebar-close" onclick="toggleSidebar()" title="Close">✕</button>
      </div>
      <div class="sidebar-body" id="positions-list">
        <p class="pos-loading">Loading…</p>
      </div>
      <div class="sidebar-refresh">
        <span class="pos-count" id="pos-count"></span>
        <button class="btn-refresh" onclick="loadPositions()">↻ Refresh</button>
      </div>
    </div>
  </aside>

  <div class="main-content">

  <!-- ── Charts ───────────────────────────────────────────────────────────── -->
  <div class="chart-wrap">
    <div class="chart-tabs">
      <button class="chart-tab active" id="tab-portfolio" onclick="switchChartTab('portfolio')">Portfolio</button>
      <button class="chart-tab"        id="tab-suggestions" onclick="switchChartTab('suggestions')">Suggestions P&amp;L</button>
    </div>
    <div id="chart-panel-portfolio">
      <canvas id="seriesChart" width="800" height="220"></canvas>
      <p id="chart-empty" style="color:var(--muted);font-size:0.8125rem;margin:0.25rem 0 0;display:none">No data yet — chart populates after the first scan completes.</p>
    </div>
    <div id="chart-panel-suggestions" style="display:none">
      <canvas id="suggestChart" width="800" height="220"></canvas>
      <p id="suggest-chart-empty" style="color:var(--muted);font-size:0.8125rem;margin:0.25rem 0 0;display:none">No resolved suggestions yet — projected P&amp;L populates as markets close.</p>
    </div>
  </div>

  <!-- ── Suggestion history ────────────────────────────────────────────────── -->
  <div class="card">
    <div class="suggestion-head">
      <h2 style="margin:0">Suggestion History</h2>
      <span class="proj-balance proj-zero" id="proj-balance-label">Projected: –</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Suggested</th>
          <th>Ticker</th>
          <th>Ask ¢</th>
          <th>Contracts</th>
          <th>Spend</th>
          <th>Rank</th>
          <th>Executed</th>
          <th>Result</th>
          <th>Outcome</th>
          <th>Projected Σ</th>
        </tr>
      </thead>
      <tbody id="suggest-rows"></tbody>
    </table>
    <p id="suggest-empty" style="color:var(--muted);font-size:0.8125rem;margin:0.5rem 0 0;display:none">No suggestions recorded yet.</p>
    <div class="page-bar" id="suggest-page-bar" style="display:none">
      <button class="page-btn" id="suggest-prev" onclick="suggestPage(-1)">← Prev</button>
      <span id="suggest-page-label"></span>
      <button class="page-btn" id="suggest-next" onclick="suggestPage(1)">Next →</button>
    </div>
  </div>

  <!-- ── Opportunities ─────────────────────────────────────────────────────── -->
  <div class="card">
    <h2>Current Opportunities</h2>
    <table>
      <thead>
        <tr>
          <th style="width:1.5rem"></th>
          <th>Event / Ticker</th>
          <th>Mid ¢</th>
          <th>Bid / Ask</th>
          <th>Closes in</th>
          <th>Markets</th>
          <th>Title</th>
          <th style="width:5rem"></th>
        </tr>
      </thead>
      <tbody id="opp-rows"></tbody>
    </table>
    <p id="opp-empty" style="color:var(--muted);font-size:0.8125rem;margin:0.5rem 0 0">No opportunities found in current scan window.</p>
    <div class="page-bar" id="opp-page-bar" style="display:none">
      <button class="page-btn" id="opp-prev" onclick="oppPage(-1)">← Prev</button>
      <span id="opp-page-label"></span>
      <button class="page-btn" id="opp-next" onclick="oppPage(1)">Next →</button>
    </div>
  </div>

  <!-- ── Event log ─────────────────────────────────────────────────────────── -->
  <div class="card">
    <h2>Event Log</h2>
    <table>
      <thead><tr><th>Time (UTC)</th><th>Kind</th><th>Detail</th></tr></thead>
      <tbody id="event-rows"></tbody>
    </table>
  </div>

  </div><!-- end .main-content -->
  </div><!-- end .page-layout -->

  <script>
    // ── Scan status & countdown ───────────────────────────────────────────────
    let nextScanAt   = null; // Date
    let lastScanAt   = null; // Date
    let scanInterval = 120;  // seconds, updated from controls

    function fmtTime(d) {
      if (!d) return '–';
      return d.toLocaleTimeString(undefined, { hour:'2-digit', minute:'2-digit', second:'2-digit' });
    }

    function updateCountdown() {
      const ring  = document.getElementById('ring-fg');
      const num   = document.getElementById('ring-num');
      const nxt   = document.getElementById('next-scan-val');
      const lst   = document.getElementById('last-scan-val');

      lst.textContent = lastScanAt ? fmtTime(lastScanAt) : '–';

      if (!nextScanAt) { num.textContent = '–'; nxt.textContent = '–'; return; }

      const secs = Math.max(0, (nextScanAt - Date.now()) / 1000);
      nxt.textContent = fmtTime(nextScanAt);
      num.textContent = secs < 1 ? '…' : Math.ceil(secs) + 's';

      // Ring arc: full at scanInterval, empty at 0
      const pct    = scanInterval > 0 ? secs / scanInterval : 0;
      const circum = 2 * Math.PI * 20; // r=20 → ≈125.66
      ring.style.strokeDasharray  = circum;
      ring.style.strokeDashoffset = circum * (1 - pct);
      ring.style.stroke = secs < 15 ? '#f5a623' : '#6eb5ff';
    }

    async function loadStatus() {
      try {
        const s = await fetch('/api/status').then(r => r.json());
        if (s.nextScanAt) nextScanAt = new Date(s.nextScanAt);
        if (s.lastScanAt) lastScanAt = new Date(s.lastScanAt);
        document.getElementById('scan-total-bar').textContent   = s.lastScanTotal   >= 0 ? s.lastScanTotal.toLocaleString()   : '–';
        document.getElementById('scan-matched-bar').textContent = s.lastScanMatched >= 0 ? s.lastScanMatched.toLocaleString() : '–';

        const fmt = c => '$' + (c / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        if (s.balanceCents != null) {
          document.getElementById('balance-bar').textContent = fmt(s.balanceCents);
        }
        document.getElementById('execute-indicator').style.display = s.executeEnabled ? 'flex' : 'none';

        const bar  = document.getElementById('skip-reason-bar');
        const text = document.getElementById('skip-reason-text');
        if (s.skipReason) {
          text.textContent  = s.skipReason;
          bar.style.display = '';
        } else {
          bar.style.display = 'none';
        }
      } catch(e) {}
    }

    // Smooth countdown tick every second
    setInterval(updateCountdown, 1000);

    // ── Sidebar ───────────────────────────────────────────────────────────────
    let sidebarOpen = false;

    function toggleSidebar() {
      sidebarOpen = !sidebarOpen;
      document.getElementById('sidebar').classList.toggle('collapsed', !sidebarOpen);
      if (sidebarOpen) loadPositions();
    }

    async function loadPositions() {
      const list    = document.getElementById('positions-list');
      const counter = document.getElementById('pos-count');
      list.innerHTML = '<p class="pos-loading">Loading…</p>';
      try {
        const positions = await fetch('/api/positions').then(r => r.json());
        if (positions.error) {
          list.innerHTML = `<p class="pos-error">Error: ${positions.error}</p>`;
          counter.textContent = '';
          return;
        }
        counter.textContent = `${positions.length} position${positions.length !== 1 ? 's' : ''}`;
        if (positions.length === 0) {
          list.innerHTML = '<p class="pos-empty">No open positions.</p>';
          return;
        }
        list.innerHTML = '';
        for (const p of positions) {
          const row = document.createElement('div');
          row.className = 'pos-row';
          const sideClass = p.side === 'yes' ? 'pos-badge-yes' : 'pos-badge-no';
          const entry = p.estEntryCents != null ? `entry ~${p.estEntryCents}¢` : '';
          const pnl   = p.realizedPnl  !== 0   ? ` · PnL $${p.realizedPnl.toFixed(2)}` : '';

          // Cost + payout if win: entry × contracts spent, profit = (100 − entry) × contracts
          let payoutHtml = '';
          if (p.estEntryCents != null) {
            const costCents   = p.estEntryCents * p.contracts;
            const profitCents = (100 - p.estEntryCents) * p.contracts;
            const totalCents  = 100 * p.contracts;
            payoutHtml =
              `<div class="pos-payout">` +
                `Cost ~$${(costCents/100).toFixed(2)} · ` +
                `🏆 +$${(profitCents/100).toFixed(2)} profit ($${(totalCents/100).toFixed(2)} total) if wins` +
              `</div>`;
          }

          // Time remaining
          const marketClosed = p.closeTime && (new Date(p.closeTime) - Date.now()) <= 0;
          let timeHtml = '';
          if (p.closeTime) {
            const msLeft = new Date(p.closeTime) - Date.now();
            if (msLeft > 0) {
              const totalMins = Math.floor(msLeft / 60000);
              const hrs  = Math.floor(totalMins / 60);
              const mins = totalMins % 60;
              const label = hrs > 0 ? `${hrs}h ${mins}m remaining` : `${mins}m remaining`;
              const cls   = totalMins < 30 ? 'critical' : totalMins < 120 ? 'urgent' : '';
              timeHtml = `<div class="pos-time${cls ? ' ' + cls : ''}">⏱ ${label}</div>`;
            } else {
              timeHtml = `<div class="pos-time critical">Closing…</div>`;
            }
          }

          const displayTitle = p.title || p.ticker;
          const titleAttr   = p.title ? `${p.ticker} — ${p.title.replace(/"/g, '&quot;')}` : p.ticker;
          const posLink     = p.url || `https://kalshi.com/markets/${(p.ticker).toLowerCase()}`;
          row.innerHTML = `
            <a href="${posLink}" target="_blank" rel="noopener" class="mkt-link pos-ticker" title="${titleAttr}">${displayTitle}</a>
            <div class="pos-meta">
              <span class="pos-badge ${sideClass}">${p.side.toUpperCase()}</span>
              <span class="pos-info">${p.contracts} cts${entry ? ' · ' + entry : ''}${pnl}</span>
              ${marketClosed ? '' : `<button class="btn-sell" onclick='openSell(${JSON.stringify(p)})'>Sell</button>`}
            </div>
            ${payoutHtml}
            ${timeHtml}`;
          list.appendChild(row);
        }
      } catch(e) {
        list.innerHTML = `<p class="pos-error">Fetch failed: ${e.message}</p>`;
        counter.textContent = '';
      }
    }

    // ── Sell dialog ───────────────────────────────────────────────────────────
    let sellInFlight = false;

    function openSell(position) {
      document.getElementById('sell-ticker').value       = position.ticker;
      document.getElementById('sell-side-display').value = position.side.toUpperCase();
      document.getElementById('sell-held').value         = position.contracts;
      document.getElementById('sell-count').value        = position.contracts;
      // Pre-fill price as est entry or a blank for user to fill
      document.getElementById('sell-price').value        = position.estEntryCents ?? '';
      const r = document.getElementById('sell-result');
      r.textContent = ''; r.className = 'modal-result';
      document.getElementById('sell-confirm').disabled = false;
      sellInFlight = false;
      document.getElementById('dlg-sell').showModal();
    }

    function closeSell() {
      document.getElementById('dlg-sell').close();
    }

    async function submitSell() {
      if (sellInFlight) return;
      sellInFlight = true;
      const btn = document.getElementById('sell-confirm');
      btn.disabled = true;
      btn.textContent = 'Selling…';
      const result = document.getElementById('sell-result');
      result.className = 'modal-result';

      const ticker       = document.getElementById('sell-ticker').value;
      const side         = document.getElementById('sell-side-display').value.toLowerCase();
      const count        = +document.getElementById('sell-count').value;
      const yesPriceCents= +document.getElementById('sell-price').value;

      try {
        const resp = await fetch('/api/execute', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ticker, side, action: 'sell', yesPriceCents, count })
        }).then(r => r.json());

        result.textContent = resp.message || (resp.success ? 'Sold' : resp.error);
        result.className   = 'modal-result ' + (resp.success ? 'ok' : 'err');
        btn.textContent    = resp.success ? '✓ Done' : '✗ Failed';
        if (resp.success) {
          setTimeout(() => { closeSell(); loadPositions(); }, 1800);
        } else {
          btn.disabled = false; sellInFlight = false;
        }
      } catch(e) {
        result.textContent = 'Request failed: ' + e.message;
        result.className   = 'modal-result err';
        btn.textContent    = 'Sell';
        btn.disabled = false; sellInFlight = false;
      }
    }

    // ── Settings modal ────────────────────────────────────────────────────────
    function openSettings() {
      document.getElementById('dlg-settings').showModal();
    }

    // ── Execute modal ─────────────────────────────────────────────────────────
    let execInFlight = false;

    function openExecute(ticker, askCents, spendCents, title, eventTicker) {
      const count = askCents > 0 ? Math.max(1, Math.floor(spendCents / askCents)) : 1;
      document.getElementById('exec-ticker').value = ticker;
      document.getElementById('exec-title').textContent = title || '';
      // Prefer the canonical URL stored with the opportunity; fall back to event page
      const _oppUrl = (window._oppUrlMap || {})[ticker];
      document.getElementById('exec-market-link').href =
        _oppUrl || `https://kalshi.com/markets/${(eventTicker || ticker).toLowerCase()}`;
      document.getElementById('exec-price').value  = askCents;
      document.getElementById('exec-count').value  = count;
      document.querySelector('input[name="exec-side"][value="yes"]').checked = true;
      const r = document.getElementById('exec-result');
      r.textContent = ''; r.className = 'modal-result';
      document.getElementById('exec-confirm').disabled = false;
      execInFlight = false;
      document.getElementById('dlg-execute').showModal();
    }

    function closeExecute() {
      document.getElementById('dlg-execute').close();
    }

    async function submitExecute() {
      if (execInFlight) return;
      execInFlight = true;
      const btn = document.getElementById('exec-confirm');
      btn.disabled = true;
      btn.textContent = 'Placing…';
      const result = document.getElementById('exec-result');
      result.className = 'modal-result';

      const ticker       = document.getElementById('exec-ticker').value;
      const yesPriceCents= +document.getElementById('exec-price').value;
      const count        = +document.getElementById('exec-count').value;
      const side         = document.querySelector('input[name="exec-side"]:checked').value;

      try {
        const resp = await fetch('/api/execute', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ticker, side, yesPriceCents, count })
        }).then(r => r.json());

        result.textContent = resp.message || (resp.success ? 'Order placed' : resp.error);
        result.className   = 'modal-result ' + (resp.success ? 'ok' : 'err');
        btn.textContent    = resp.success ? '✓ Done' : '✗ Failed';
        if (resp.success) setTimeout(closeExecute, 2000);
        else { btn.disabled = false; execInFlight = false; }
      } catch(e) {
        result.textContent = 'Request failed: ' + e.message;
        result.className   = 'modal-result err';
        btn.textContent    = 'Place Order';
        btn.disabled = false; execInFlight = false;
      }
    }

    // ── Force scan ────────────────────────────────────────────────────────────
    let forceInFlight = false;
    async function forceScan() {
      if (forceInFlight) return;
      forceInFlight = true;
      const btn = document.getElementById('btn-force');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Triggering…';
      try {
        const res = await fetch('/api/scan/force', { method: 'POST' }).then(r => r.json());
        btn.innerHTML = '✓ ' + (res.message || 'Triggered');
        btn.classList.add('scanning');
        // Reset after 3 s
        setTimeout(() => {
          btn.disabled = false;
          btn.innerHTML = '▶ Force Scan';
          btn.classList.remove('scanning');
          forceInFlight = false;
        }, 3000);
      } catch(e) {
        btn.innerHTML = '✗ Error';
        setTimeout(() => {
          btn.disabled = false;
          btn.innerHTML = '▶ Force Scan';
          forceInFlight = false;
        }, 2000);
      }
    }

    // ── Controls ──────────────────────────────────────────────────────────────
    let dirty = false;
    function markDirty() {
      dirty = true;
      const ex = document.getElementById('ctrl-execute').checked;
      document.getElementById('exec-warning').style.display = ex ? '' : 'none';
    }

    async function loadControls() {
      try {
        const c = await fetch('/api/controls').then(r => r.json());
        document.getElementById('ctrl-execute').checked = c.executeEnabled;
        document.getElementById('ctrl-interval').value  = c.scanIntervalSeconds;
        document.getElementById('ctrl-maxbets').value   = c.maxBetsPerHour;
        document.getElementById('ctrl-spend').value     = ((c.spendPerBetCents || 500) / 100).toFixed(2);
        document.getElementById('ctrl-maxhours').value  = c.maxHoursToClose;
        document.getElementById('ctrl-margin').value    = c.nearFiftyMarginCents;
        document.getElementById('ctrl-minpayout').value = c.minPayoutMarginCents ?? 0;
        document.getElementById('ctrl-maxpos').value    = c.maxOpenPositions;
        document.getElementById('exec-warning').style.display = c.executeEnabled ? '' : 'none';
        scanInterval = c.scanIntervalSeconds || 120;
        dirty = false;
      } catch(e) { console.warn('controls load failed', e); }
    }

    async function saveControls() {
      const spendDollars = parseFloat(document.getElementById('ctrl-spend').value) || 5;
      const body = {
        executeEnabled:       document.getElementById('ctrl-execute').checked,
        scanIntervalSeconds:  +document.getElementById('ctrl-interval').value,
        maxBetsPerHour:       +document.getElementById('ctrl-maxbets').value,
        spendPerBetCents:     Math.round(spendDollars * 100),
        maxHoursToClose:      +document.getElementById('ctrl-maxhours').value,
        nearFiftyMarginCents:  +document.getElementById('ctrl-margin').value,
        minPayoutMarginCents:  +document.getElementById('ctrl-minpayout').value,
        maxOpenPositions:      +document.getElementById('ctrl-maxpos').value,
      };
      try {
        await fetch('/api/controls', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        scanInterval = body.scanIntervalSeconds;
        dirty = false;
        const s = document.getElementById('save-status');
        s.style.display = '';
        setTimeout(() => s.style.display = 'none', 2500);
      } catch(e) { alert('Save failed: ' + e); }
    }

    // ── Opportunities ─────────────────────────────────────────────────────────
    const OPP_PAGE_SIZE = 10;
    let oppPage_ = 0;
    let oppGroups = [];           // sorted array of { key, markets[] }
    const expandedGroups = new Set(); // persists across refreshes

    function buildOppGroups(opps) {
      const map = new Map();
      for (const o of opps) {
        const key = o.eventTicker || o.ticker;
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(o);
      }
      const groups = [...map.entries()].map(([key, markets]) => {
        markets.sort((a, b) => Math.abs(a.midCents - 50) - Math.abs(b.midCents - 50));
        return { key, markets };
      });
      groups.sort((a, b) =>
        Math.abs(a.markets[0].midCents - 50) - Math.abs(b.markets[0].midCents - 50));
      return groups;
    }

    function midBar(mid) {
      const pct = mid;
      return `<div class="mid-bar"><div class="mid-fill" style="width:${pct}%;left:0"></div></div>`;
    }

    function renderOppPage() {
      const tb    = document.getElementById('opp-rows');
      const empty = document.getElementById('opp-empty');
      const bar   = document.getElementById('opp-page-bar');
      const label = document.getElementById('opp-page-label');

      tb.innerHTML = '';

      if (oppGroups.length === 0) {
        empty.style.display = '';
        bar.style.display = 'none';
        return;
      }
      empty.style.display = 'none';

      const totalPages = Math.ceil(oppGroups.length / OPP_PAGE_SIZE);
      oppPage_ = Math.max(0, Math.min(oppPage_, totalPages - 1));
      const slice = oppGroups.slice(oppPage_ * OPP_PAGE_SIZE, (oppPage_ + 1) * OPP_PAGE_SIZE);

      // Pagination controls
      if (totalPages > 1) {
        bar.style.display = '';
        label.textContent = `Page ${oppPage_ + 1} of ${totalPages}  (${oppGroups.length} events)`;
        document.getElementById('opp-prev').disabled = oppPage_ === 0;
        document.getElementById('opp-next').disabled = oppPage_ === totalPages - 1;
      } else {
        bar.style.display = 'none';
      }

      for (let gi = 0; gi < slice.length; gi++) {
        const g       = slice[gi];
        const best    = g.markets[0];
        const mid     = best.midCents;
        const hrs     = best.hoursToClose.toFixed(1);
        const hrsClass= +hrs < 3 ? 'badge-warn' : 'badge-ok';
        const isOpen  = expandedGroups.has(g.key);
        const multi   = g.markets.length > 1;
        // Top pick = first group on page 0 (highest payout in the scan)
        const isTopPick = (gi === 0 && oppPage_ === 0);

        // current spend budget (read live from controls input, fallback 500¢)
        const spendCents = Math.round((parseFloat(document.getElementById('ctrl-spend')?.value) || 5) * 100);

        // ── Group header row ──────────────────────────────────────────────
        const hdr = document.createElement('tr');
        hdr.className = 'group-hdr' + (isTopPick ? ' top-pick' : '');
        hdr.dataset.key = g.key;
        const topBadge = isTopPick ? '<span class="badge-top">TOP PICK</span>' : '';
        hdr.innerHTML =
          `<td><span class="expand-btn${isOpen ? ' open' : ''}">${multi ? '▶' : ''}</span></td>` +
          `<td><a href="${best.url || 'https://kalshi.com/markets/' + g.key.toLowerCase()}" target="_blank" rel="noopener" class="mkt-link"><code style="font-size:0.72rem">${g.key}</code></a>${topBadge}</td>` +
          `<td><strong>${mid}¢</strong> ${midBar(mid)}</td>` +
          `<td style="color:var(--muted)">${best.yesBidCents}¢ / ${best.yesAskCents}¢</td>` +
          `<td><span class="badge ${hrsClass}">${hrs}h</span></td>` +
          `<td style="color:var(--muted)">${g.markets.length}</td>` +
          `<td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${best.title}">${best.title}</td>` +
          `<td></td>`;

        // Only the expand area triggers toggle; execute button gets its own listener below
        if (multi) {
          hdr.querySelector('.expand-btn').addEventListener('click', (e) => { e.stopPropagation(); toggleGroup(g.key); });
          hdr.addEventListener('click', () => toggleGroup(g.key));
        }

        // Execute button on header uses the best (first) market's ask
        const hdrExecBtn = document.createElement('button');
        hdrExecBtn.className = 'btn-exec' + (isTopPick ? ' btn-exec-primary' : '');
        hdrExecBtn.textContent = 'Execute';
        hdrExecBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          openExecute(best.ticker, best.yesAskCents, spendCents, best.title, best.eventTicker);
        });
        hdr.querySelector('td:last-child').appendChild(hdrExecBtn);

        tb.appendChild(hdr);

        // ── Child rows (one per market, hidden unless expanded) ───────────
        if (multi) {
          for (const o of g.markets) {
            const child = document.createElement('tr');
            child.className = 'child-row' + (isOpen ? ' visible' : '');
            child.dataset.group = g.key;
            const cm  = o.midCents;
            const chs = o.hoursToClose.toFixed(1);
            const cc  = +chs < 3 ? 'badge-warn' : 'badge-ok';
            child.innerHTML =
              `<td></td>` +
              `<td><a href="${o.url || 'https://kalshi.com/markets/' + (o.eventTicker || o.ticker).toLowerCase()}" target="_blank" rel="noopener" class="mkt-link"><code style="font-size:0.72rem">${o.ticker}</code></a></td>` +
              `<td>${cm}¢ ${midBar(cm)}</td>` +
              `<td>${o.yesBidCents}¢ / ${o.yesAskCents}¢</td>` +
              `<td><span class="badge ${cc}">${chs}h</span></td>` +
              `<td></td>` +
              `<td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${o.title}">${o.title}</td>` +
              `<td></td>`;

            const childExecBtn = document.createElement('button');
            childExecBtn.className = 'btn-exec';
            childExecBtn.textContent = 'Execute';
            childExecBtn.addEventListener('click', () => openExecute(o.ticker, o.yesAskCents, spendCents, o.title, o.eventTicker));
            child.querySelector('td:last-child').appendChild(childExecBtn);

            tb.appendChild(child);
          }
        }
      }
    }

    function toggleGroup(key) {
      if (expandedGroups.has(key)) expandedGroups.delete(key);
      else expandedGroups.add(key);
      // Toggle arrow + child visibility without full re-render
      document.querySelectorAll(`.group-hdr[data-key="${key}"] .expand-btn`).forEach(el => {
        el.classList.toggle('open', expandedGroups.has(key));
      });
      document.querySelectorAll(`.child-row[data-group="${key}"]`).forEach(el => {
        el.classList.toggle('visible', expandedGroups.has(key));
      });
    }

    function oppPage(dir) {
      oppPage_ += dir;
      renderOppPage();
    }

    function renderOpportunities(opps) {
      oppGroups = buildOppGroups(opps);
      // Build ticker→url lookup for execute modal
      window._oppUrlMap = {};
      for (const o of opps) if (o.url) window._oppUrlMap[o.ticker] = o.url;
      document.getElementById('scan-matched-bar').textContent = oppGroups.length > 0
        ? `${opps.length} (${oppGroups.length} events)` : '0';
      renderOppPage();
    }

    // ── Chart ─────────────────────────────────────────────────────────────────
    let seriesChart = null;

    function buildOrUpdateChart(points) {
      const empty = document.getElementById('chart-empty');
      if (!points || points.length === 0) {
        if (empty) empty.style.display = '';
        return;
      }
      if (empty) empty.style.display = 'none';

      const labels  = points.map(p => {
        const t = new Date((p.unix || 0) * 1000);
        return t.toLocaleTimeString(undefined, { hour:'2-digit', minute:'2-digit' });
      });
      const bal       = points.map(p => +((Number(p.balance_cents) || 0) / 100).toFixed(2));
      const spent     = points.map(p => +((Number(p.spent_cents)   || 0) / 100).toFixed(2));
      const bets      = points.map(p =>   Number(p.bets_placed)    || 0);
      const contracts = points.map(p =>   Number(p.contract_count) || 0);

      const shared = { tension: 0.3, fill: false, pointRadius: 0, borderWidth: 2 };
      const datasets = [
        { ...shared, label: 'Balance ($)',    data: bal,       yAxisID: 'yUsd',  borderColor: '#3ecf8e', backgroundColor: 'rgba(62,207,142,0.08)' },
        { ...shared, label: 'Spent ($)',      data: spent,     yAxisID: 'yUsd',  borderColor: '#f5a623', backgroundColor: 'rgba(245,166,35,0.08)', borderDash: [4,3] },
        { ...shared, label: 'Bets placed',    data: bets,      yAxisID: 'yCount', borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,0.08)', stepped: 'after' },
        { ...shared, label: 'Contracts held', data: contracts, yAxisID: 'yCount', borderColor: '#6eb5ff', backgroundColor: 'rgba(110,181,255,0.08)', stepped: 'after' },
      ];

      const scaleBase = { grid: { color: '#2a3544' }, ticks: { color: '#8b9aab' } };
      const options = {
        responsive: true, maintainAspectRatio: true, animation: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: { ...scaleBase, ticks: { ...scaleBase.ticks, maxTicksLimit: 10 } },
          yUsd:   { ...scaleBase, position: 'left',  title: { display: true, text: 'USD ($)', color: '#8b9aab', font: { size: 11 } } },
          yCount: { ...scaleBase, position: 'right', title: { display: true, text: 'Count', color: '#a78bfa', font: { size: 11 } },
            grid: { drawOnChartArea: false },
            ticks: { color: '#a78bfa', precision: 0 } },
        },
        plugins: {
          legend: { labels: { color: '#e7ecf3', boxWidth: 14, padding: 16 } },
          tooltip: {
            callbacks: {
              label: ctx => {
                const v = ctx.parsed.y;
                return ctx.dataset.yAxisID === 'yCount'
                  ? ` ${ctx.dataset.label}: ${v}`
                  : ` ${ctx.dataset.label}: $${v.toFixed(2)}`;
              }
            }
          }
        }
      };

      const ctx = document.getElementById('seriesChart');
      if (!seriesChart) {
        seriesChart = new Chart(ctx, { type: 'line', data: { labels, datasets }, options });
      } else {
        seriesChart.data.labels = labels;
        seriesChart.data.datasets.forEach((ds, i) => ds.data = datasets[i].data);
        seriesChart.update('none');
      }
    }

    // ── Chart tab switcher ────────────────────────────────────────────────────
    let activeChartTab = 'portfolio';
    function switchChartTab(tab) {
      activeChartTab = tab;
      document.getElementById('chart-panel-portfolio').style.display   = tab === 'portfolio'   ? '' : 'none';
      document.getElementById('chart-panel-suggestions').style.display = tab === 'suggestions' ? '' : 'none';
      document.getElementById('tab-portfolio').classList.toggle('active',   tab === 'portfolio');
      document.getElementById('tab-suggestions').classList.toggle('active', tab === 'suggestions');
    }

    // ── Suggestion P&L chart ──────────────────────────────────────────────────
    let suggestChart = null;

    function buildOrUpdateSuggestChart(suggestions) {
      const empty = document.getElementById('suggest-chart-empty');
      const resolved = suggestions.filter(s => s.resolution !== null);
      if (resolved.length === 0) {
        if (empty) empty.style.display = '';
        return;
      }
      if (empty) empty.style.display = 'none';

      const labels   = resolved.map(s => new Date(s.suggestedAt).toLocaleDateString(undefined, { month:'short', day:'numeric' })
                                        + ' ' + new Date(s.suggestedAt).toLocaleTimeString(undefined, { hour:'2-digit', minute:'2-digit' }));
      const outcome  = resolved.map(s => +((s.outcomeCents || 0) / 100).toFixed(2));
      const cumul    = resolved.map(s => +((s.cumulativeCents || 0) / 100).toFixed(2));

      const shared = { tension: 0.2, fill: false, pointRadius: 3, borderWidth: 2 };
      const datasets = [
        { ...shared, label: 'Per-bet outcome ($)', data: outcome, borderColor: '#6eb5ff',
          backgroundColor: outcome.map(v => v >= 0 ? 'rgba(62,207,142,0.7)' : 'rgba(242,81,81,0.7)'),
          type: 'bar', yAxisID: 'yUsd' },
        { ...shared, label: 'Projected balance ($)', data: cumul, borderColor: '#3ecf8e',
          backgroundColor: 'rgba(62,207,142,0.08)', type: 'line', pointRadius: 2, yAxisID: 'yUsd',
          stepped: 'after' },
      ];

      const scaleBase = { grid: { color: '#2a3544' }, ticks: { color: '#8b9aab' } };
      const options = {
        responsive: true, maintainAspectRatio: true, animation: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: { ...scaleBase, ticks: { ...scaleBase.ticks, maxTicksLimit: 12 } },
          yUsd: { ...scaleBase, position: 'left', title: { display: true, text: 'USD ($)', color: '#8b9aab', font: { size: 11 } } },
        },
        plugins: {
          legend: { labels: { color: '#e7ecf3', boxWidth: 14, padding: 16 } },
          tooltip: { callbacks: { label: ctx => ` ${ctx.dataset.label}: $${ctx.parsed.y.toFixed(2)}` } }
        }
      };

      const ctx = document.getElementById('suggestChart');
      if (!suggestChart) {
        suggestChart = new Chart(ctx, { type: 'bar', data: { labels, datasets }, options });
      } else {
        suggestChart.data.labels = labels;
        suggestChart.data.datasets.forEach((ds, i) => { ds.data = datasets[i].data; if (i === 0) ds.backgroundColor = datasets[i].backgroundColor; });
        suggestChart.update('none');
      }
    }

    // ── Suggestion history table ──────────────────────────────────────────────
    const SUGGEST_PAGE_SIZE = 15;
    let suggestRows_ = [];   // newest-first full list
    let suggestPage_ = 0;

    function suggestPage(dir) {
      suggestPage_ += dir;
      renderSuggestPage();
    }

    function renderSuggestPage() {
      const tb    = document.getElementById('suggest-rows');
      const empty = document.getElementById('suggest-empty');
      const bar   = document.getElementById('suggest-page-bar');
      const label = document.getElementById('suggest-page-label');
      tb.innerHTML = '';

      if (suggestRows_.length === 0) {
        empty.style.display = '';
        bar.style.display = 'none';
        return;
      }
      empty.style.display = 'none';

      const totalPages = Math.ceil(suggestRows_.length / SUGGEST_PAGE_SIZE);
      suggestPage_ = Math.max(0, Math.min(suggestPage_, totalPages - 1));
      const slice = suggestRows_.slice(suggestPage_ * SUGGEST_PAGE_SIZE, (suggestPage_ + 1) * SUGGEST_PAGE_SIZE);

      if (totalPages > 1) {
        bar.style.display = '';
        label.textContent = `Page ${suggestPage_ + 1} of ${totalPages}  (${suggestRows_.length} suggestions)`;
        document.getElementById('suggest-prev').disabled = suggestPage_ === 0;
        document.getElementById('suggest-next').disabled = suggestPage_ === totalPages - 1;
      } else {
        bar.style.display = 'none';
      }

      for (const s of slice) {
        const tr = document.createElement('tr');

        const time    = new Date(s.suggestedAt).toLocaleString(undefined, { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' });
        const spend   = `$${(s.spendCents / 100).toFixed(2)}`;
        const rankCls = s.scanRank === 1 ? 'sug-rank1' : '';
        const rankLbl = s.scanRank === 1 ? '★ #1' : `#${s.scanRank}`;

        const execCell = s.executeError
          ? `<span class="sug-error" title="${s.executeError.replace(/"/g,'&quot;')}">✕ Error</span>`
          : s.executed
            ? `<span class="sug-exec">✓ Yes</span>`
            : `<span class="sug-pend">—</span>`;

        let resCell, outcomeCell, cumulCell;
        if (s.resolution === 'yes') {
          resCell     = `<span class="sug-won">✓ YES</span>`;
          outcomeCell = `<span class="sug-won">+$${((s.outcomeCents||0)/100).toFixed(2)}</span>`;
          cumulCell   = `<span class="${s.cumulativeCents >= 0 ? 'sug-won':'sug-lost'}">$${(s.cumulativeCents/100).toFixed(2)}</span>`;
        } else if (s.resolution === 'no') {
          resCell     = `<span class="sug-lost">✗ NO</span>`;
          outcomeCell = `<span class="sug-lost">-$${(Math.abs(s.outcomeCents||0)/100).toFixed(2)}</span>`;
          cumulCell   = `<span class="${s.cumulativeCents >= 0 ? 'sug-won':'sug-lost'}">$${(s.cumulativeCents/100).toFixed(2)}</span>`;
        } else {
          resCell     = `<span class="sug-pend">Pending</span>`;
          outcomeCell = `<span class="sug-pend">—</span>`;
          cumulCell   = `<span class="sug-pend">—</span>`;
        }

        tr.innerHTML =
          `<td style="white-space:nowrap;font-size:0.75rem">${time}</td>` +
          `<td><a href="${s.url || 'https://kalshi.com/markets/' + (s.eventTicker||s.ticker).toLowerCase()}" target="_blank" rel="noopener" class="mkt-link"><code style="font-size:0.7rem">${s.ticker}</code></a></td>` +
          `<td>${s.yesAskCents}¢</td>` +
          `<td>${s.contractCount}</td>` +
          `<td>${spend}</td>` +
          `<td class="${rankCls}">${rankLbl}</td>` +
          `<td>${execCell}</td>` +
          `<td>${resCell}</td>` +
          `<td>${outcomeCell}</td>` +
          `<td>${cumulCell}</td>`;
        tb.appendChild(tr);
      }
    }

    function renderSuggestions(suggestions) {
      if (!suggestions) suggestions = [];

      // Newest first
      suggestRows_ = [...suggestions].reverse();

      // Stay on page 0 when new data arrives (keeps newest visible)
      suggestPage_ = 0;
      renderSuggestPage();

      // Update projected balance label
      const resolved = suggestions.filter(s => s.resolution !== null);
      if (resolved.length > 0) {
        const last  = resolved[resolved.length - 1];
        const cents = last.cumulativeCents || 0;
        const lbl   = document.getElementById('proj-balance-label');
        const cls   = cents > 0 ? 'proj-pos' : cents < 0 ? 'proj-neg' : 'proj-zero';
        lbl.className   = `proj-balance ${cls}`;
        lbl.textContent = `Projected: ${cents >= 0 ? '+' : ''}$${(cents/100).toFixed(2)} (${resolved.length} resolved)`;
      }

      buildOrUpdateSuggestChart(suggestions);
    }

    // ── Event log ─────────────────────────────────────────────────────────────
    function renderEvents(events) {
      const tb = document.getElementById('event-rows');
      tb.innerHTML = '';
      for (const ev of events) {
        const tr  = document.createElement('tr');
        const k   = ev.kind || '';
        const cls = 'kind kind-' + k.replace(/[^a-z0-9_-]/gi,'_');
        const detail = document.createElement('td');
        const pre    = document.createElement('pre');
        pre.textContent = JSON.stringify(ev, null, 2);
        tr.innerHTML = `<td style="white-space:nowrap">${(ev.ts_iso||'').replace('T',' ').slice(0,19)}</td><td class="${cls}">${k}</td>`;
        tr.appendChild(detail);
        detail.appendChild(pre);
        tb.appendChild(tr);
      }
    }

    // ── Poll loop ─────────────────────────────────────────────────────────────
    async function poll() {
      try {
        const [events, series, opps, suggestions] = await Promise.all([
          fetch('/api/events').then(r => r.json()),
          fetch('/api/series').then(r => r.json()),
          fetch('/api/opportunities').then(r => r.json()),
          fetch('/api/suggestions').then(r => r.json()),
        ]);
        renderEvents(events);
        renderOpportunities(opps);
        buildOrUpdateChart(series);
        renderSuggestions(suggestions);
        if (!dirty) await loadControls();
        await loadStatus();
        updateCountdown();
      } catch(e) {
        console.warn('poll error', e);
      }
    }

    // ── User badge ────────────────────────────────────────────────────────────
    function signOut() { window.location.href = '/logout'; }

    fetch('/api/me').then(r => r.json()).then(me => {
      if (me.authenticated) {
        document.getElementById('header-user-name').textContent = me.name || '';
        document.getElementById('header-user-badge').style.display = 'flex';
      }
    }).catch(() => {});

    loadControls();
    loadStatus().then(updateCountdown);
    poll();
    setInterval(poll, 30000);
  </script>
</body>
</html>
""";
}
