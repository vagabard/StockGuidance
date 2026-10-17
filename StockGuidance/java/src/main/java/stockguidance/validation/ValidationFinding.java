package stockguidance.validation;

import java.time.LocalDate;
import java.util.List;
import java.util.Set;

public record ValidationFinding(
        String category,
        String check,
        String detail,
        LocalDate sessionDate,
        LocalDate dependencySessionDate,
        List<String> tickers,
        List<Integer> recordIds
) {
    private static final Set<String> BLOCKING = Set.of("missing", "incomplete", "invalid", "unverified", "failed");

    public ValidationFinding {
        if (category == null || category.isBlank() || check == null || check.isBlank() || detail == null || detail.isBlank()) {
            throw new IllegalArgumentException("Validation finding is incomplete");
        }
        tickers = List.copyOf(tickers);
        recordIds = List.copyOf(recordIds);
    }

    public boolean blocking() {
        return BLOCKING.contains(category);
    }
}
