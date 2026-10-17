package stockguidance.interchange;

import java.time.LocalDate;
import java.util.List;
import java.util.Set;

public record Coverage(
        List<LocalDate> observedDates,
        List<LocalDate> verifiedDates,
        List<LocalDate> holes,
        String provenance,
        String providerState
) {
    public Coverage {
        observedDates = List.copyOf(observedDates);
        verifiedDates = List.copyOf(verifiedDates);
        holes = List.copyOf(holes);
        if (!isSortedUnique(observedDates) || !isSortedUnique(verifiedDates) || !isSortedUnique(holes)) {
            throw new IllegalArgumentException("Coverage dates must be sorted and unique");
        }
        if (!Set.copyOf(observedDates).containsAll(verifiedDates)) {
            throw new IllegalArgumentException("Verified coverage must be observed coverage");
        }
        if (provenance == null || provenance.isBlank() || providerState == null || providerState.isBlank()) {
            throw new IllegalArgumentException("Coverage provenance is required");
        }
    }

    private static boolean isSortedUnique(List<LocalDate> values) {
        for (int index = 1; index < values.size(); index++) {
            if (!values.get(index - 1).isBefore(values.get(index))) {
                return false;
            }
        }
        return true;
    }
}
