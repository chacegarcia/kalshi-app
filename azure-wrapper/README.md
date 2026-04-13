# Kalshi Bot — Azure Wrapper (C#)

ASP.NET Core 8 web application that runs the Kalshi trading bot loop and serves a real-time monitoring dashboard. Deployed to **Azure Container Apps**.

---

## Project structure

```
azure-wrapper/
├── Program.cs                  # Routes + embedded dashboard HTML/JS/CSS
├── KalshiBotWrapper.csproj
├── Dockerfile
├── appsettings.json
├── kalshi_private_key.pem      # RSA private key (not committed)
├── .env.azure.example          # Environment variable reference
│
├── Bot/                        # Trading logic
│   ├── TradingSettings.cs      # All env-var config
│   ├── MarketScanner.cs        # Scan for near-50¢ opportunities
│   ├── OrderExecution.cs       # Place / dry-run orders
│   ├── RiskManager.cs          # Pre-trade risk checks
│   ├── PortfolioService.cs     # Fetch balance & positions
│   ├── AutoSellLoop.cs         # Automatic profit-taking exits
│   └── ...
├── Kalshi/                     # Kalshi REST client
│   ├── KalshiAuth.cs           # RSA-PSS SHA-256 signing
│   ├── KalshiRestClient.cs     # Authenticated HTTP client (retry)
│   └── KalshiModels.cs         # API DTOs
├── Dashboard/
│   └── DashboardStore.cs       # Thread-safe in-memory dashboard state
└── Services/
    └── BotRunnerService.cs     # Background hosted service (bot loop)
```

---

## Dashboard

Navigate to the container's public URL (port 8080) to view the live dashboard:

- **Opportunity monitor** — markets closing within the configured window, prices near 50¢
- **Positions sidebar** — open positions with side, contract count, estimated entry price, realized PnL, and a Sell button
- **Portfolio chart** — balance, spend, bets placed, and contract count over time
- **Runtime controls** — adjust scan interval, spend per bet, position limits on the fly
- **Force Scan** — trigger an immediate market scan

---

## Build & deploy

The Docker build context must be the **repo root** (not `azure-wrapper/`) because the Dockerfile copies only the `azure-wrapper/` subtree.

### 1. Build and push to ACR

```bash
az acr build --registry cgjprg --image kalshi-bot:latest --file azure-wrapper/Dockerfile .
```

### 2. Update the container app image + environment

```bash
az containerapp update \
  --name kalshi-bot \
  --resource-group cgjp-rg \
  --image cgjprg.azurecr.io/kalshi-bot:latest \
  --set-env-vars \
    KALSHI_ENV=prod \
    LIVE_TRADING=false \
    DRY_RUN=true \
    KALSHI_API_KEY_ID=secretref:kalshi-api-key-id \
    KALSHI_PRIVATE_KEY_PEM=secretref:kalshi-private-key-pem \
    MICROSOFT_CLIENT_ID=<client-id> \
    MICROSOFT_TENANT_ID=<tenant-id> \
    MICROSOFT_CLIENT_SECRET=secretref:microsoft-client-secret \
    AUTH_ALLOWED_EMAILS=you@example.com
```

### One-liner: build + update

```bash
az acr build --registry cgjprg --image kalshi-bot:latest --file azure-wrapper/Dockerfile . && az containerapp update --name kalshi-bot --resource-group cgjp-rg --image cgjprg.azurecr.io/kalshi-bot:latest --set-env-vars KALSHI_ENV=prod LIVE_TRADING=false DRY_RUN=true KALSHI_API_KEY_ID=secretref:kalshi-api-key-id KALSHI_PRIVATE_KEY_PEM=secretref:kalshi-private-key-pem MICROSOFT_CLIENT_ID=<client-id> MICROSOFT_TENANT_ID=<tenant-id> MICROSOFT_CLIENT_SECRET=secretref:microsoft-client-secret AUTH_ALLOWED_EMAILS=you@example.com
```

`&&` ensures the build completes before the update runs.

---

## Update environment variables

Use `az containerapp update` with `--set-env-vars` to add or change plain-text variables, and `--secrets` + `--set-env-vars` for sensitive values. Changes take effect after the revision restarts.

### Set plain-text variables

```bash
az containerapp update \
  --name kalshi-bot \
  --resource-group cgjp-rg \
  --set-env-vars \
    KALSHI_ENV=prod \
    LIVE_TRADING=false \
    DRY_RUN=true \
    AUTH_ALLOWED_EMAILS=alice@example.com,bob@example.com
```

### Set sensitive variables as secrets

Secrets are stored encrypted in the container app and injected as env vars at runtime. Use this for anything credential-like.

```bash
# 1. Store the values as named secrets
az containerapp secret set \
  --name kalshi-bot \
  --resource-group cgjp-rg \
  --secrets \
    kalshi-api-key-id=<key-id> \
    kalshi-private-key-pem="$(cat azure-wrapper/kalshi_private_key.pem)" \
    microsoft-client-secret=<secret> \
    github-client-secret=<secret>

# 2. Wire each secret to its environment variable
az containerapp update \
  --name kalshi-bot \
  --resource-group cgjp-rg \
  --set-env-vars \
    KALSHI_API_KEY_ID=secretref:kalshi-api-key-id \
    KALSHI_PRIVATE_KEY_PEM=secretref:kalshi-private-key-pem \
    MICROSOFT_CLIENT_SECRET=secretref:microsoft-client-secret \
    GITHUB_CLIENT_SECRET=secretref:github-client-secret
```

### View current environment

```bash
# All env vars (shows secret refs, not values)
az containerapp show \
  --name kalshi-bot \
  --resource-group cgjp-rg \
  --query "properties.template.containers[0].env" \
  -o table

# List secret names
az containerapp secret list \
  --name kalshi-bot \
  --resource-group cgjp-rg \
  -o table
```

### Restart after env changes

Environment variable updates create a new revision automatically via `az containerapp update`. If you edited secrets separately, force a restart:

```bash
az containerapp revision restart \
  --name kalshi-bot \
  --resource-group cgjp-rg \
  --revision $(az containerapp revision list \
      --name kalshi-bot \
      --resource-group cgjp-rg \
      --query "[?properties.active].name | [0]" \
      -o tsv)
```

---

## Local development

```bash
# Build image locally
docker build -t kalshi-bot -f azure-wrapper/Dockerfile .

# Run with env vars
docker run -p 8080:8080 \
  -e KALSHI_API_KEY_ID=<your-key-id> \
  -e KALSHI_PRIVATE_KEY_PEM="$(cat kalshi_private_key.pem)" \
  -e KALSHI_ENV=demo \
  kalshi-bot
```

Or run directly with the .NET SDK:

```bash
cd azure-wrapper
KALSHI_API_KEY_ID=<key> KALSHI_ENV=demo dotnet run
```

---

## Key environment variables

| Variable | Default | Description |
|---|---|---|
| `KALSHI_API_KEY_ID` | *(required)* | RSA key ID from Kalshi dashboard |
| `KALSHI_PRIVATE_KEY_PATH` | | Path to PEM file |
| `KALSHI_PRIVATE_KEY_PEM` | | Inline PEM (alternative to path) |
| `KALSHI_ENV` | `demo` | `demo` or `prod` |
| `LIVE_TRADING` | `false` | Enable real order placement |
| `DRY_RUN` | `true` | Simulate orders without API calls |
| `MAX_EXPOSURE_CENTS` | `50000` | Total position exposure ceiling |
| `MAX_CONTRACTS_PER_MARKET` | `10` | Per-market contract cap |
| `MAX_DAILY_DRAWDOWN_USD` | `25` | Session loss limit |
| `Bot__Command` | `run` | Bot mode: `run`, `scan`, `list-markets`, `cancel-all` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | | Azure Application Insights |

See `.env.azure.example` for the full list.

---

## Authentication (optional)

Auth is opt-in. The dashboard runs unauthenticated if neither provider is configured.

### Microsoft Entra ID (Azure AD)

1. In the [Azure portal](https://portal.azure.com), go to **Azure Active Directory → App registrations → New registration**.
2. Name it (e.g. `kalshi-bot-dashboard`). Set the redirect URI:
   ```
   https://<your-container-app-domain>/signin-oidc
   ```
3. Under **Certificates & secrets**, create a client secret.
4. Set these environment variables on the container app:

   | Variable | Value |
   |---|---|
   | `MICROSOFT_CLIENT_ID` | Application (client) ID |
   | `MICROSOFT_CLIENT_SECRET` | The secret value |
   | `MICROSOFT_TENANT_ID` | Directory (tenant) ID, or `common` for any Microsoft account |

### GitHub OAuth

1. Go to **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**.
2. Set the **Authorization callback URL**:
   ```
   https://<your-container-app-domain>/signin-github
   ```
3. Set these environment variables:

   | Variable | Value |
   |---|---|
   | `GITHUB_CLIENT_ID` | Client ID |
   | `GITHUB_CLIENT_SECRET` | Client secret |

Both providers can be active simultaneously — the `/login` page will show both buttons.

### Email whitelist

By default any authenticated account is allowed in. To restrict access, set:

```
AUTH_ALLOWED_EMAILS=alice@example.com,bob@company.com
```

Accounts whose email is not on the list are rejected **before** the session cookie is issued and redirected back to `/login` with an "Access denied" message. If the variable is absent or empty, all authenticated accounts are allowed.

> **GitHub note:** the user's GitHub account must have a **public** primary email set, or the whitelist check will always fail (private-only accounts have no email exposed via the OAuth user endpoint).

---

## Diagnostic endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/api/positions` | Parsed open positions (used by the sidebar) |
| `GET` | `/api/positions/debug` | Raw Kalshi API response + parse diagnostics (use this to troubleshoot the sidebar) |
| `GET` | `/api/opportunities` | Current scan results |
| `GET` | `/api/events` | Last 500 bot events |
| `GET` | `/api/series` | Portfolio chart data points |
| `GET` | `/api/status` | Scan timing / countdown |
| `GET` | `/api/controls` | Current runtime controls |
| `POST` | `/api/controls` | Update runtime controls |
| `POST` | `/api/scan/force` | Trigger immediate scan |
| `POST` | `/api/execute` | Place a manual order |

### Debugging the positions sidebar

If the sidebar shows "No open positions" or an error when you know positions exist, hit:

```
GET /api/positions/debug
```

This returns:
- `kalshiEnv` / `restBaseUrl` — confirms which Kalshi environment is being used
- `rawResponse` — the exact JSON Kalshi returned (check for unexpected field formats)
- `totalParsed` — total positions in the raw response
- `nonZeroCount` — positions with a non-zero contract count after parsing
- `parsedPositions` — the fully-deserialized position objects

Common causes:
- **Auth failure** → `error` field will contain `Kalshi API 401: ...`
- **Wrong env** (`demo` vs `prod`) → `totalParsed` will be 0 if you have no demo positions
- **JSON parse mismatch** → `totalParsed` > 0 but `nonZeroCount` = 0 means the `position` field deserialized as null
