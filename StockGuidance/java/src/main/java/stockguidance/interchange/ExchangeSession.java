package stockguidance.interchange;

import java.time.Instant;
import java.time.LocalDate;

public record ExchangeSession(LocalDate exchangeDate, Instant closeAt) {
    public ExchangeSession {
        if (exchangeDate == null || closeAt == null) {
            throw new IllegalArgumentException("Exchange session requires a date and close time");
        }
    }
}
