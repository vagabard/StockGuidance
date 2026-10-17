package stockguidance.validation;

import com.fasterxml.jackson.databind.JsonNode;
import stockguidance.json.CanonicalJson;
import stockguidance.json.ContractException;
import stockguidance.json.JsonSupport;

import java.time.Instant;
import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class ValidationRequest {
    private static final Set<String> ROOT_FIELDS = Set.of("schema_version", "operation", "source", "calendar", "scope", "source_evidence", "history", "max_rows");
    private static final Set<String> SOURCE_FIELDS = Set.of("provider", "endpoint", "market", "universe", "adjustment", "interval");
    private static final Set<String> CALENDAR_FIELDS = Set.of("id", "version", "sessions");
    private static final Set<String> SESSION_FIELDS = Set.of("exchange_date", "sequence", "opens_at", "close_at");
    private static final Set<String> SCOPE_FIELDS = Set.of("start", "end", "as_of");
    private static final Set<String> EVIDENCE_FIELDS = Set.of("exchange_date", "state", "available_at", "keys", "content_id", "records_id", "record_count");
    private static final Set<String> HISTORY_FIELDS = Set.of("history_start", "history_provenance", "unresolved_limits");

    private final JsonNode source;
    private final JsonNode history;
    private final String operation;
    private final String calendarId;
    private final String calendarVersion;
    private final List<SessionSpec> sessions;
    private final LocalDate start;
    private final LocalDate end;
    private final Instant asOf;
    private final Map<LocalDate, SourceEvidence> sourceEvidence;
    private final int maxRows;

    private ValidationRequest(JsonNode source, JsonNode history, String operation, String calendarId, String calendarVersion,
                              List<SessionSpec> sessions, LocalDate start, LocalDate end, Instant asOf,
                              Map<LocalDate, SourceEvidence> sourceEvidence, int maxRows) {
        this.source = source;
        this.history = history;
        this.operation = operation;
        this.calendarId = calendarId;
        this.calendarVersion = calendarVersion;
        this.sessions = List.copyOf(sessions);
        this.start = start;
        this.end = end;
        this.asOf = asOf;
        this.sourceEvidence = Map.copyOf(sourceEvidence);
        this.maxRows = maxRows;
    }

    public static ValidationRequest read(java.nio.file.Path path) {
        return decode(JsonSupport.read(path));
    }

    public static ValidationRequest decode(JsonNode root) {
        fields(root, ROOT_FIELDS);
        if (integer(root, "schema_version") != 1) {
            throw new ContractException("Unsupported validation request version");
        }
        String operation = requiredText(root, "operation");
        if (!operation.equals("raw_derived_reconciliation")) {
            throw new ContractException("Unsupported validation operation");
        }
        JsonNode source = object(root, "source");
        fields(source, SOURCE_FIELDS);
        JsonNode calendar = object(root, "calendar");
        fields(calendar, CALENDAR_FIELDS);
        String calendarId = requiredText(calendar, "id");
        String calendarVersion = requiredText(calendar, "version");
        List<SessionSpec> sessions = sessions(array(calendar, "sessions"), calendarId);
        JsonNode scope = object(root, "scope");
        fields(scope, SCOPE_FIELDS);
        LocalDate start = date(scope, "start");
        LocalDate end = date(scope, "end");
        if (start.isAfter(end)) {
            throw new ContractException("Validation scope is reversed");
        }
        Instant asOf = instant(scope, "as_of");
        Map<LocalDate, SourceEvidence> evidence = evidence(array(root, "source_evidence"));
        JsonNode history = object(root, "history");
        fields(history, HISTORY_FIELDS);
        int maxRows = root.has("max_rows") ? integer(root, "max_rows") : 100_000;
        if (maxRows < 1 || maxRows > 1_000_000) {
            throw new ContractException("Validation row bound is outside the supported range");
        }
        if (sessions.stream().noneMatch(item -> !item.exchangeDate().isBefore(start) && !item.exchangeDate().isAfter(end))) {
            throw new ContractException("Validation scope has no calendar sessions");
        }
        return new ValidationRequest(source.deepCopy(), history.deepCopy(), operation, calendarId, calendarVersion, sessions, start, end, asOf, evidence, maxRows);
    }

    public JsonNode source() {
        return source.deepCopy();
    }

    public JsonNode history() {
        return history.deepCopy();
    }

    public String operation() {
        return operation;
    }

    public String calendarId() {
        return calendarId;
    }

    public String calendarVersion() {
        return calendarVersion;
    }

    public List<SessionSpec> sessions() {
        return sessions;
    }

    public LocalDate start() {
        return start;
    }

    public LocalDate end() {
        return end;
    }

    public Instant asOf() {
        return asOf;
    }

    public Map<LocalDate, SourceEvidence> sourceEvidence() {
        return sourceEvidence;
    }

    public int maxRows() {
        return maxRows;
    }

    public String sourceKey() {
        return CanonicalJson.sha256(source);
    }

    public record SessionSpec(LocalDate exchangeDate, int sequence, Instant opensAt, Instant closeAt) {
        public SessionSpec {
            if (exchangeDate == null || opensAt == null || closeAt == null || !opensAt.isBefore(closeAt)) {
                throw new ContractException("Invalid authoritative calendar session");
            }
        }
    }

    public record SourceEvidence(LocalDate exchangeDate, String state, Instant availableAt, List<String> keys,
                                 String contentId, String recordsId, int recordCount) {
        public SourceEvidence {
            if (exchangeDate == null || state == null || state.isBlank() || availableAt == null || keys == null || recordCount < 0) {
                throw new ContractException("Invalid source evidence");
            }
            keys = List.copyOf(keys);
            if (new HashSet<>(keys).size() != keys.size() || keys.stream().anyMatch(item -> item == null || item.isBlank())) {
                throw new ContractException("Source evidence keys must be unique text");
            }
            if (contentId == null || !contentId.matches("[0-9a-f]{64}") || recordsId == null || !recordsId.matches("[0-9a-f]{64}")) {
                throw new ContractException("Invalid source content identity");
            }
        }
    }

    private static List<SessionSpec> sessions(JsonNode value, String calendarId) {
        List<SessionSpec> result = new ArrayList<>();
        LocalDate previousDate = null;
        int previousSequence = Integer.MIN_VALUE;
        for (JsonNode item : value) {
            fields(item, SESSION_FIELDS);
            LocalDate exchangeDate = date(item, "exchange_date");
            int sequence = integer(item, "sequence");
            if (previousDate != null && (!previousDate.isBefore(exchangeDate) || sequence != previousSequence + 1)) {
                throw new ContractException("Authoritative calendar sessions must be chronological");
            }
            result.add(new SessionSpec(exchangeDate, sequence, instant(item, "opens_at"), instant(item, "close_at")));
            previousDate = exchangeDate;
            previousSequence = sequence;
        }
        if (result.isEmpty()) {
            throw new ContractException("Authoritative calendar is empty");
        }
        return result;
    }

    private static Map<LocalDate, SourceEvidence> evidence(JsonNode value) {
        Map<LocalDate, SourceEvidence> result = new LinkedHashMap<>();
        for (JsonNode item : value) {
            fields(item, EVIDENCE_FIELDS);
            LocalDate date = date(item, "exchange_date");
            if (result.containsKey(date)) {
                throw new ContractException("Duplicate source evidence session");
            }
            result.put(date, new SourceEvidence(date, requiredText(item, "state"), instant(item, "available_at"), texts(array(item, "keys")), requiredText(item, "content_id"), requiredText(item, "records_id"), integer(item, "record_count")));
        }
        return result;
    }

    private static List<String> texts(JsonNode value) {
        List<String> result = new ArrayList<>();
        for (JsonNode item : value) {
            if (!item.isTextual() || item.textValue().isBlank()) {
                throw new ContractException("Expected nonempty text array");
            }
            result.add(item.textValue());
        }
        return result;
    }

    private static void fields(JsonNode value, Set<String> expected) {
        if (value == null || !value.isObject()) {
            throw new ContractException("Validation object is required");
        }
        Set<String> actual = new HashSet<>();
        Iterator<String> names = value.fieldNames();
        while (names.hasNext()) {
            actual.add(names.next());
        }
        if (!actual.equals(expected)) {
            throw new ContractException("Incomplete or unsupported validation fields");
        }
    }

    private static JsonNode object(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || !value.isObject()) {
            throw new ContractException("Expected validation object");
        }
        return value;
    }

    private static JsonNode array(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || !value.isArray()) {
            throw new ContractException("Expected validation array");
        }
        return value;
    }

    private static int integer(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || !value.isIntegralNumber() || !value.canConvertToInt()) {
            throw new ContractException("Expected validation integer");
        }
        return value.intValue();
    }

    private static String requiredText(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || !value.isTextual() || value.textValue().isBlank()) {
            throw new ContractException("Expected validation text");
        }
        return value.textValue();
    }

    private static LocalDate date(JsonNode root, String name) {
        try {
            return LocalDate.parse(requiredText(root, name));
        } catch (DateTimeParseException error) {
            throw new ContractException("Invalid validation exchange date");
        }
    }

    private static Instant instant(JsonNode root, String name) {
        String value = requiredText(root, name);
        if (!value.endsWith("Z")) {
            throw new ContractException("Validation timestamps must use UTC Z serialization");
        }
        try {
            return Instant.parse(value);
        } catch (DateTimeParseException error) {
            throw new ContractException("Invalid validation timestamp");
        }
    }
}
