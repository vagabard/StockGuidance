package stockguidance.interchange;

import java.math.BigDecimal;
import java.time.LocalDate;

public record Observation(
        LocalDate exchangeDate,
        int tickerId,
        BigDecimal value,
        Receipt receipt,
        String availabilityPolicy,
        String revision
) {
    public Observation {
        if (exchangeDate == null || tickerId <= 0 || receipt == null || availabilityPolicy == null || availabilityPolicy.isBlank()) {
            throw new IllegalArgumentException("Observation contract is incomplete");
        }
    }
}
