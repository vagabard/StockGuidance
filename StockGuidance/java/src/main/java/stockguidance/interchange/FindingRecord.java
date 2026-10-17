package stockguidance.interchange;

import java.time.LocalDate;
import java.util.List;

public record FindingRecord(
        String category,
        String check,
        String detail,
        LocalDate sessionDate,
        List<Integer> tickerIds
) {
    public FindingRecord {
        if (category == null || category.isBlank() || check == null || check.isBlank() || detail == null || detail.isBlank()) {
            throw new IllegalArgumentException("Finding is incomplete");
        }
        tickerIds = List.copyOf(tickerIds);
        if (tickerIds.stream().anyMatch(id -> id == null || id <= 0)) {
            throw new IllegalArgumentException("Finding ticker IDs must be positive");
        }
    }
}
