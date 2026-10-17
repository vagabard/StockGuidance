package stockguidance.interchange;

import java.time.Instant;

public record Receipt(Instant observedAt, Instant availableAt, String contentId) {
    public Receipt {
        if (observedAt == null || availableAt == null || contentId == null || contentId.isBlank()) {
            throw new IllegalArgumentException("Receipt evidence is incomplete");
        }
        if (availableAt.isBefore(observedAt)) {
            throw new IllegalArgumentException("Receipt availability precedes observation");
        }
    }
}
