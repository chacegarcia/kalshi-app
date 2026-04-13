# Build context must be the repo root (one level above azure-wrapper/).
# Run from J:\GitHub\kalshi-app\ :
#   docker build -t kalshi-bot -f azure-wrapper/Dockerfile .

# ── Stage 1: build the C# project ────────────────────────────────────────────
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /build
COPY azure-wrapper/KalshiBotWrapper.csproj ./
RUN dotnet restore
COPY azure-wrapper/ ./
RUN dotnet publish -c Release -o /publish --no-restore

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS runtime
WORKDIR /app

COPY --from=build /publish .

# ── Runtime configuration ─────────────────────────────────────────────────────
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
ENV ASPNETCORE_HTTP_PORTS=8080

EXPOSE 8080

ENTRYPOINT ["dotnet", "KalshiBotWrapper.dll"]
