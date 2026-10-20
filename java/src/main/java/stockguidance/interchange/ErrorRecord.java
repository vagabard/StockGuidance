package stockguidance.interchange;

import java.time.LocalDate;

public record ErrorRecord(String code, String message, LocalDate exchangeDate) {
    public ErrorRecord {
        if (code == null || code.isBlank() || message == null || message.isBlank()) {
            throw new IllegalArgumentException("Interchange error is incomplete");
        }
    }
}
