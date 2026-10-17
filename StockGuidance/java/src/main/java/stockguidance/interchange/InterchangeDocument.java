package stockguidance.interchange;

import java.util.HashSet;
import java.util.List;
import java.util.Set;

public record InterchangeDocument(
        int schemaVersion,
        SourceIdentity source,
        String calendarId,
        String calendarVersion,
        List<Integer> tickerIds,
        List<ExchangeSession> sessions,
        Coverage coverage,
        List<Observation> observations,
        List<ErrorRecord> errors,
        List<FindingRecord> findings
) {
    public InterchangeDocument {
        if (schemaVersion != 1 || source == null || calendarId == null || calendarId.isBlank() || calendarVersion == null || calendarVersion.isBlank() || coverage == null) {
            throw new IllegalArgumentException("Unsupported interchange contract");
        }
        tickerIds = List.copyOf(tickerIds);
        sessions = List.copyOf(sessions);
        observations = List.copyOf(observations);
        errors = List.copyOf(errors);
        findings = List.copyOf(findings);
        if (tickerIds.stream().anyMatch(id -> id == null || id <= 0) || new HashSet<>(tickerIds).size() != tickerIds.size()) {
            throw new IllegalArgumentException("Ticker IDs must be positive and unique");
        }
        for (int index = 1; index < sessions.size(); index++) {
            if (!sessions.get(index - 1).exchangeDate().isBefore(sessions.get(index).exchangeDate())) {
                throw new IllegalArgumentException("Exchange sessions must be chronological");
            }
        }
        Set<Integer> knownTickers = Set.copyOf(tickerIds);
        if (observations.stream().anyMatch(item -> !knownTickers.contains(item.tickerId()))) {
            throw new IllegalArgumentException("Observation uses an unknown ticker ID");
        }
    }
}
