package stockguidance.validation;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
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

    private final JsonObject source;
    private final JsonObject history;
    private final String operation;
    private final String calendarId;
    private final String calendarVersion;
    private final List<SessionSpec> sessions;
    private final LocalDate start;
    private final LocalDate end;
    private final Instant asOf;
    private final Map<LocalDate, SourceEvidence> sourceEvidence;
    private final int maxRows;

    private ValidationRequest(JsonObject source, JsonObject history, String operation, String calendarId, String calendarVersion,
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

    public static ValidationRequest decode(JsonObject root) {
        fields(root, ROOT_FIELDS);
        if (integer(root, "schema_version") != 1) {
            throw new ContractException("Unsupported validation request version");
        }
        String operation = requiredText(root, "operation");
        if (!operation.equals("raw_derived_reconciliation")) {
            throw new ContractException("Unsupported validation operation");
        }
        JsonObject source = object(root, "source");
        fields(source, SOURCE_FIELDS);
        JsonObject calendar = object(root, "calendar");
        fields(calendar, CALENDAR_FIELDS);
        String calendarId = requiredText(calendar, "id");
        String calendarVersion = requiredText(calendar, "version");
        List<SessionSpec> sessions = sessions(array(calendar, "sessions"), calendarId);
        JsonObject scope = object(root, "scope");
        fields(scope, SCOPE_FIELDS);
        LocalDate start = date(scope, "start");
        LocalDate end = date(scope, "end");
        if (start.isAfter(end)) {
            throw new ContractException("Validation scope is reversed");
        }
        Instant asOf = instant(scope, "as_of");
        Map<LocalDate, SourceEvidence> evidence = evidence(array(root, "source_evidence"));
        JsonObject history = object(root, "history");
        fields(history, HISTORY_FIELDS);
        int maxRows = root.containsKey("max_rows") ? integer(root, "max_rows") : 100_000;
        if (maxRows < 1 || maxRows > 1_000_000) {
            throw new ContractException("Validation row bound is outside the supported range");
        }
        if (sessions.stream().noneMatch(item -> !item.exchangeDate().isBefore(start) && !item.exchangeDate().isAfter(end))) {
            throw new ContractException("Validation scope has no calendar sessions");
        }
        return new ValidationRequest((JsonObject) JsonSupport.deepCopy(source), (JsonObject) JsonSupport.deepCopy(history), operation, calendarId, calendarVersion, sessions, start, end, asOf, evidence, maxRows);
    }

    public JsonObject source() {
        return (JsonObject) JsonSupport.deepCopy(source);
    }

    public JsonObject history() {
        return (JsonObject) JsonSupport.deepCopy(history);
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

    private static List<SessionSpec> sessions(JsonArray value, String calendarId) {
        List<SessionSpec> result = new ArrayList<>();
        LocalDate previousDate = null;
        int previousSequence = Integer.MIN_VALUE;
        for (Object rawItem : value) {
            if (!(rawItem instanceof JsonObject item)) throw new ContractException("Validation session must be an object");
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

    private static Map<LocalDate, SourceEvidence> evidence(JsonArray value) {
        Map<LocalDate, SourceEvidence> result = new LinkedHashMap<>();
        for (Object rawItem : value) {
            if (!(rawItem instanceof JsonObject item)) throw new ContractException("Validation source evidence must be an object");
            fields(item, EVIDENCE_FIELDS);
            LocalDate date = date(item, "exchange_date");
            if (result.containsKey(date)) {
                throw new ContractException("Duplicate source evidence session");
            }
            result.put(date, new SourceEvidence(date, requiredText(item, "state"), instant(item, "available_at"), texts(array(item, "keys")), requiredText(item, "content_id"), requiredText(item, "records_id"), integer(item, "record_count")));
        }
        return result;
    }

    private static List<String> texts(JsonArray value) {
        List<String> result = new ArrayList<>();
        for (Object item : value) {
            if (!JsonSupport.isString(item) || JsonSupport.stringValue(item).isBlank()) {
                throw new ContractException("Expected nonempty text array");
            }
            result.add(JsonSupport.stringValue(item));
        }
        return result;
    }

    private static void fields(JsonObject value, Set<String> expected) {
        if (value == null) {
            throw new ContractException("Validation object is required");
        }
        Set<String> actual = new HashSet<>();
        actual.addAll(value.keySet());
        if (!actual.equals(expected)) {
            throw new ContractException("Incomplete or unsupported validation fields");
        }
    }

    private static JsonObject object(JsonObject root, String name) {
        Object value = root.get(name);
        if (!(value instanceof JsonObject object)) {
            throw new ContractException("Expected validation object");
        }
        return object;
    }

    private static JsonArray array(JsonObject root, String name) {
        Object value = root.get(name);
        if (!(value instanceof JsonArray array)) {
            throw new ContractException("Expected validation array");
        }
        return array;
    }

    private static int integer(JsonObject root, String name) {
        Object value = root.get(name);
        if (!JsonSupport.isIntegralNumber(value) || !JsonSupport.fitsInt(value)) {
            throw new ContractException("Expected validation integer");
        }
        return JsonSupport.integerValue(value);
    }

    private static String requiredText(JsonObject root, String name) {
        Object value = root.get(name);
        if (!JsonSupport.isString(value) || JsonSupport.stringValue(value).isBlank()) {
            throw new ContractException("Expected validation text");
        }
        return JsonSupport.stringValue(value);
    }

    private static LocalDate date(JsonObject root, String name) {
        try {
            return LocalDate.parse(requiredText(root, name));
        } catch (DateTimeParseException error) {
            throw new ContractException("Invalid validation exchange date");
        }
    }

    private static Instant instant(JsonObject root, String name) {
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
