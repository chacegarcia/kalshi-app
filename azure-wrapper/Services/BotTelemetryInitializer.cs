using Microsoft.ApplicationInsights.Channel;
using Microsoft.ApplicationInsights.Extensibility;

namespace KalshiBotWrapper.Services;

/// <summary>
/// Tags every Application Insights telemetry item with the active bot command
/// (e.g. "llm-trade", "run") so you can filter traces in the portal by command.
/// </summary>
public sealed class BotTelemetryInitializer : ITelemetryInitializer
{
    private readonly string _command;

    public BotTelemetryInitializer(string command) => _command = command;

    public void Initialize(ITelemetry telemetry)
    {
        telemetry.Context.GlobalProperties.TryAdd("BotCommand", _command);
        telemetry.Context.Cloud.RoleName = "kalshi-bot-wrapper";
    }
}
