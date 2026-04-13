namespace KalshiBotWrapper.Configuration;

/// <summary>
/// Wrapper-level settings (appsettings.json / Bot__ env vars).
/// Trading settings (KALSHI_*, TRADE_*, etc.) are read directly from environment
/// variables by TradingSettings.FromEnvironment() inside BotRunnerService.
/// </summary>
public sealed class BotConfiguration
{
    public const string Section = "Bot";

    /// <summary>
    /// The kalshi-bot sub-command to run.
    /// Valid values: run | scan | llm-trade | list-markets | cancel-all
    /// </summary>
    public string Command { get; set; } = "run";

    /// <summary>
    /// Additional CLI-style arguments appended after the command.
    /// Supported flags: --dry-run (for run), --execute, --loop, --interval N (for llm-trade)
    /// </summary>
    public string ExtraArgs { get; set; } = "--dry-run";

    /// <summary>
    /// Filename of the Kalshi RSA private key packaged alongside this binary.
    /// At runtime BotRunnerService looks for it next to the DLL and sets
    /// KALSHI_PRIVATE_KEY_PATH if found.
    /// </summary>
    public string PrivateKeyFileName { get; set; } = "kalshi_private_key.pem";

    /// <summary>
    /// Optional Azure Key Vault URI. If set, secrets listed in KeyVaultSecrets
    /// are fetched at startup and injected as environment variables.
    /// </summary>
    public string? KeyVaultUri { get; set; }

    /// <summary>
    /// Comma-separated Key Vault secret names. Hyphens converted to underscores.
    /// </summary>
    public string KeyVaultSecrets { get; set; } = string.Empty;
}
