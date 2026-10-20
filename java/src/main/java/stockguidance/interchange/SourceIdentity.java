package stockguidance.interchange;

public record SourceIdentity(
        String provider,
        String endpoint,
        String market,
        String universe,
        String adjustment,
        String interval
) {
    public SourceIdentity {
        requireText(provider, "provider");
        requireText(endpoint, "endpoint");
        requireText(market, "market");
        requireText(universe, "universe");
        requireText(adjustment, "adjustment");
        requireText(interval, "interval");
    }

    private static void requireText(String value, String name) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("Invalid source " + name);
        }
    }
}
