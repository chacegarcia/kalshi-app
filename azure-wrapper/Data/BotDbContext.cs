using Microsoft.EntityFrameworkCore;

namespace KalshiBotWrapper.Data;

public sealed class BotDbContext(DbContextOptions<BotDbContext> options) : DbContext(options)
{
    public DbSet<EventHistoryEntity>      EventHistory      => Set<EventHistoryEntity>();
    public DbSet<SuggestionHistoryEntity> SuggestionHistory => Set<SuggestionHistoryEntity>();
    public DbSet<BotControlsEntity>       BotControls       => Set<BotControlsEntity>();
    public DbSet<PortfolioSeriesEntity>   PortfolioSeries   => Set<PortfolioSeriesEntity>();

    protected override void OnModelCreating(ModelBuilder mb)
    {
        mb.Entity<EventHistoryEntity>(e =>
        {
            e.ToTable("event_history");
            e.HasKey(x => x.Id);
            e.Property(x => x.Id).UseIdentityColumn();
            e.Property(x => x.Kind).HasMaxLength(100);
            e.Property(x => x.KalshiEnv).HasMaxLength(10);
            e.HasIndex(x => x.CreatedAt).HasDatabaseName("ix_event_history_created_at");
        });

        mb.Entity<SuggestionHistoryEntity>(e =>
        {
            e.ToTable("suggestion_history");
            e.HasKey(x => x.Id);
            e.Property(x => x.Id).UseIdentityColumn();
            e.Property(x => x.Ticker).HasMaxLength(100);
            e.Property(x => x.EventTicker).HasMaxLength(100);
            e.Property(x => x.Title).HasMaxLength(500);
            e.Property(x => x.Url).HasMaxLength(500);
            e.Property(x => x.Resolution).HasMaxLength(10);
            e.Property(x => x.KalshiEnv).HasMaxLength(10);
            e.HasIndex(x => x.SuggestionId).HasDatabaseName("ix_suggestion_history_suggestion_id");
            e.HasIndex(x => x.CreatedAt).HasDatabaseName("ix_suggestion_history_created_at");
            e.HasIndex(x => x.Ticker).HasDatabaseName("ix_suggestion_history_ticker");
        });

        mb.Entity<BotControlsEntity>(e =>
        {
            e.ToTable("bot_controls");
            e.HasKey(x => x.Id);
            // No identity — we manage the singleton Id=1 ourselves
            e.Property(x => x.Id).ValueGeneratedNever();
        });

        mb.Entity<PortfolioSeriesEntity>(e =>
        {
            e.ToTable("portfolio_series");
            e.HasKey(x => x.Id);
            e.Property(x => x.Id).UseIdentityColumn();
            e.Property(x => x.KalshiEnv).HasMaxLength(10);
            e.HasIndex(x => x.RecordedAt).HasDatabaseName("ix_portfolio_series_recorded_at");
        });
    }
}
