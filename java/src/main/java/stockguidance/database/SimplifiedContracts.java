package stockguidance.database;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
import stockguidance.json.ContractException;
import stockguidance.json.JsonSupport;

import java.nio.file.Path;
import java.time.Instant;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Pattern;

/** Immutable Python/Java value contracts for the Stage 3 replacement schema. */
public final class SimplifiedContracts {
    public static final int SCHEMA_VERSION = 1;
    public static final int SCOPE_VERSION = 1;
    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");
    private static final Set<String> REPAIR_FIELDS = Set.of("open", "high", "low", "close", "volume", "vwap", "transactions");

    private SimplifiedContracts() { }

    public enum OperationKind { INITIAL_IMPORT("initial_import"), FORCED_REPLACEMENT("forced_replacement"), DERIVED_REPAIR("derived_repair"), RECOVERY("recovery");
        private final String value;
        OperationKind(String value) { this.value = value; }
        public String value() { return value; }
        static OperationKind parse(String value) { for (OperationKind item : values()) if (item.value.equals(value)) return item; throw new ContractException("Unknown operation kind"); }
    }

    public enum UnitKind { RAW_SESSION("raw_session"), REPAIR_ROW("repair_row"), DELTA_SESSION("delta_session"), ZSCORE_SESSION("zscore_session"), CACHE_REVISION("cache_revision");
        private final String value;
        UnitKind(String value) { this.value = value; }
        public String value() { return value; }
        static UnitKind parse(String value) { for (UnitKind item : values()) if (item.value.equals(value)) return item; throw new ContractException("Unknown work-unit kind"); }
    }

    public enum DataState { GOOD(1, "good"), BAD(2, "bad"), MISSING(3, "missing"), INTERPOLATED(4, "interpolated");
        private final int id; private final String value;
        DataState(int id, String value) { this.id = id; this.value = value; }
        public int id() { return id; }
        public String value() { return value; }
        static DataState parse(String value) { for (DataState item : values()) if (item.value.equals(value)) return item; throw new ContractException("Unknown data state"); }
    }

    public enum CalculationStatus { READY("ready"), INSUFFICIENT_HISTORY("insufficient_history"), MISSING_INPUT("missing_input"), INVALID_INPUT("invalid_input"), NOT_YET_AVAILABLE("not_yet_available"), ZERO_VARIANCE("zero_variance");
        private final String value;
        CalculationStatus(String value) { this.value = value; }
        public String value() { return value; }
    }

    public record SourceConfiguration(String configurationKey, String sourceKey, String sourceFamily, String endpoint,
                                       String market, String universe, String intervalCode, String calendarId,
                                       boolean rawPriceSplitAdjusted, boolean rawPriceDividendAdjusted,
                                       String rawPriceVendorDefinedAdjustment, Instant createdAt) {
        public SourceConfiguration {
            sha(configurationKey, "configuration_key"); sha(sourceKey, "source_key");
            text(sourceFamily, "source_family"); text(endpoint, "endpoint"); text(market, "market");
            text(universe, "universe"); text(intervalCode, "interval_code"); text(calendarId, "calendar_id");
            if (createdAt == null) throw new ContractException("created_at is required");
        }
    }

    public record SessionCompletion(long dateId, LocalDate transactionDate, boolean rawComplete, Instant rawAvailableAt,
                                    Instant rawCompletedAt, String rawRevision, boolean deltaComplete,
                                    Instant deltaCompletedAt) {
        public SessionCompletion {
            id(dateId, "date_id"); if (transactionDate == null) throw new ContractException("transaction_date is required");
            if (rawComplete && (rawCompletedAt == null || rawRevision == null)) throw new ContractException("completed raw session is missing evidence");
            if (rawRevision != null) sha(rawRevision, "raw_revision");
            if (deltaComplete && deltaCompletedAt == null) throw new ContractException("completed delta session is missing evidence");
        }
    }

    public record RepairMetadata(long dateId, long tickerId, String revision, DataState state,
                                 List<String> originalValues, List<String> effectiveValues, List<String> invalidFields,
                                 String interpolationMethod, Long priorDateId, Long followingDateId,
                                 List<List<String>> endpointValues, Instant observedAt, Instant availableAt,
                                 Instant endpointsAvailableAt, Instant recordedAt) {
        public RepairMetadata {
            id(dateId, "date_id"); id(tickerId, "ticker_id"); sha(revision, "revision");
            if (state == null || originalValues == null || originalValues.size() != 7 || invalidFields == null) throw new ContractException("Invalid repair metadata");
            if (effectiveValues != null && effectiveValues.size() != 7) throw new ContractException("Invalid effective values");
            if (!REPAIR_FIELDS.containsAll(invalidFields) || new HashSet<>(invalidFields).size() != invalidFields.size()) throw new ContractException("Invalid repair fields");
            if (recordedAt == null) throw new ContractException("recorded_at is required");
            originalValues = List.copyOf(originalValues); effectiveValues = effectiveValues == null ? null : List.copyOf(effectiveValues);
            invalidFields = List.copyOf(invalidFields);
            if (endpointValues != null) endpointValues = endpointValues.stream().map(List::copyOf).toList();
        }
    }

    public record PreparedPayload(String payloadId, LocalDate sessionDate, Path temporaryPath, String sha256, String rawId,
                                  Instant availableAt, Instant preparedAt) {
        public PreparedPayload {
            text(payloadId, "payload_id"); if (sessionDate == null || temporaryPath == null) throw new ContractException("Invalid prepared payload");
            sha(sha256, "sha256"); sha(rawId, "raw_id"); if (availableAt == null || preparedAt == null) throw new ContractException("Invalid payload timestamps");
        }
    }

    public record WorkUnit(String unitKind, Long dateId, Long tickerId, LocalDate sessionDate, String seriesKey, Integer window) {
        public WorkUnit {
            UnitKind kind = UnitKind.parse(unitKind); unitKind = kind.value();
            if (dateId != null) id(dateId, "date_id"); if (tickerId != null) id(tickerId, "ticker_id");
            if (seriesKey != null) sha(seriesKey, "series_key"); if (window != null && window < 2) throw new ContractException("Invalid window");
            if (kind == UnitKind.RAW_SESSION && (dateId == null || sessionDate == null)) throw new ContractException("raw_session requires date and ID");
            if (kind == UnitKind.REPAIR_ROW && (dateId == null || tickerId == null)) throw new ContractException("repair_row requires IDs");
            if (kind == UnitKind.DELTA_SESSION && (dateId == null || sessionDate == null)) throw new ContractException("delta_session requires date and ID");
            if (kind == UnitKind.ZSCORE_SESSION && (dateId == null || sessionDate == null || seriesKey == null || window == null)) throw new ContractException("zscore_session requires identifiers");
        }
    }

    public record ClearScope(List<WorkUnit> raw, List<WorkUnit> repairs, List<WorkUnit> deltas,
                             List<WorkUnit> zscores, List<WorkUnit> completion) {
        public ClearScope {
            raw = copyUnique(raw, "raw"); repairs = copyUnique(repairs, "repairs"); deltas = copyUnique(deltas, "deltas");
            zscores = copyUnique(zscores, "zscores"); completion = copyUnique(completion, "completion");
        }
    }

    public record CacheInvalidation(String revisionScope, List<LocalDate> sessionDates) {
        public CacheInvalidation {
            text(revisionScope, "revision_scope"); if (sessionDates == null || new HashSet<>(sessionDates).size() != sessionDates.size()) throw new ContractException("Invalid cache dates");
            sessionDates = List.copyOf(sessionDates);
        }
    }

    public record PendingScope(int scopeVersion, OperationKind operationKind, String configurationKey, String calendarId,
                               Instant requestedAt, Instant cutoff, List<PreparedPayload> payloads, ClearScope clear,
                               List<WorkUnit> rebuild, List<CacheInvalidation> cacheInvalidations) {
        public PendingScope {
            if (scopeVersion != SCOPE_VERSION || operationKind == null) throw new ContractException("Unsupported pending scope");
            sha(configurationKey, "configuration_key"); text(calendarId, "calendar_id");
            if (requestedAt == null || cutoff == null || payloads == null || clear == null || rebuild == null || cacheInvalidations == null) throw new ContractException("Incomplete pending scope");
            if (new HashSet<>(payloads.stream().map(PreparedPayload::payloadId).toList()).size() != payloads.size()) throw new ContractException("Duplicate payload ID");
            rebuild = copyUnique(rebuild, "rebuild"); payloads = List.copyOf(payloads); cacheInvalidations = List.copyOf(cacheInvalidations);
        }
    }

    public record FailureDetail(String code, String detail, Instant failedAt) {
        public FailureDetail { text(code, "failure code"); text(detail, "failure detail"); if (failedAt == null) throw new ContractException("failed_at is required"); }
    }

    public record PendingUpdate(long operationId, PendingScope scope, Instant createdAt, Instant clearingStartedAt,
                                boolean affectedRowsCleared, FailureDetail failure) {
        public PendingUpdate { id(operationId, "operation_id"); if (scope == null || createdAt == null) throw new ContractException("Incomplete pending update"); }
    }

    public static JsonObject encodeScope(PendingScope scope) {
        if (scope == null) throw new ContractException("Pending scope is required");
        JsonObject root = JsonSupport.object(); root.put("scope_version", scope.scopeVersion()); root.put("operation_kind", scope.operationKind().value());
        root.put("configuration_key", scope.configurationKey()); root.put("calendar_id", scope.calendarId()); root.put("requested_at", instant(scope.requestedAt())); root.put("cutoff", instant(scope.cutoff()));
        JsonArray payloads = JsonSupport.putArray(root, "payloads"); for (PreparedPayload payload : scope.payloads()) { JsonObject item = JsonSupport.addObject(payloads); item.put("payload_id", payload.payloadId()); item.put("session_date", payload.sessionDate().toString()); item.put("temporary_path", payload.temporaryPath().toString()); item.put("sha256", payload.sha256()); item.put("raw_id", payload.rawId()); item.put("available_at", instant(payload.availableAt())); item.put("prepared_at", instant(payload.preparedAt())); }
        root.put("clear", encodeClear(scope.clear())); JsonArray rebuild = JsonSupport.putArray(root, "rebuild"); for (WorkUnit unit : scope.rebuild()) rebuild.add(encodeUnit(unit));
        JsonArray invalidations = JsonSupport.putArray(root, "cache_invalidations"); for (CacheInvalidation invalidation : scope.cacheInvalidations()) { JsonObject item = JsonSupport.addObject(invalidations); item.put("revision_scope", invalidation.revisionScope()); JsonArray dates = JsonSupport.putArray(item, "session_dates"); invalidation.sessionDates().forEach(value -> dates.add(value.toString())); }
        return root;
    }

    public static PendingScope decodeScope(JsonObject root) {
        fields(root, Set.of("scope_version", "operation_kind", "configuration_key", "calendar_id", "requested_at", "cutoff", "payloads", "clear", "rebuild", "cache_invalidations"));
        List<PreparedPayload> payloads = new ArrayList<>(); for (Object value : array(root, "payloads")) { JsonObject item = object(value); fields(item, Set.of("payload_id", "session_date", "temporary_path", "sha256", "raw_id", "available_at", "prepared_at")); payloads.add(new PreparedPayload(text(item, "payload_id"), LocalDate.parse(text(item, "session_date")), Path.of(text(item, "temporary_path")), text(item, "sha256"), text(item, "raw_id"), parseInstant(text(item, "available_at")), parseInstant(text(item, "prepared_at")))); }
        JsonObject clear = object(root.get("clear")); fields(clear, Set.of("raw", "repairs", "deltas", "zscores", "completion"));
        List<CacheInvalidation> invalidations = new ArrayList<>();
        for (Object value : array(root, "cache_invalidations")) {
            JsonObject item = object(value); fields(item, Set.of("revision_scope", "session_dates"));
            List<LocalDate> dates = new ArrayList<>(); for (Object session : array(item, "session_dates")) dates.add(LocalDate.parse(text(session)));
            invalidations.add(new CacheInvalidation(text(item, "revision_scope"), dates));
        }
        return new PendingScope(intValue(root.get("scope_version")), OperationKind.parse(text(root, "operation_kind")), text(root, "configuration_key"), text(root, "calendar_id"), parseInstant(text(root, "requested_at")), parseInstant(text(root, "cutoff")), payloads, new ClearScope(units(clear, "raw"), units(clear, "repairs"), units(clear, "deltas"), units(clear, "zscores"), units(clear, "completion")), units(root, "rebuild"), invalidations);
    }

    private static JsonObject encodeClear(ClearScope clear) { JsonObject root = JsonSupport.object(); root.put("raw", encodeUnits(clear.raw())); root.put("repairs", encodeUnits(clear.repairs())); root.put("deltas", encodeUnits(clear.deltas())); root.put("zscores", encodeUnits(clear.zscores())); root.put("completion", encodeUnits(clear.completion())); return root; }
    private static JsonArray encodeUnits(List<WorkUnit> units) { JsonArray result = JsonSupport.array(); units.forEach(unit -> result.add(encodeUnit(unit))); return result; }
    private static JsonObject encodeUnit(WorkUnit unit) { JsonObject result = JsonSupport.object(); result.put("unit_kind", unit.unitKind()); result.put("date_id", unit.dateId()); result.put("ticker_id", unit.tickerId()); result.put("session_date", unit.sessionDate() == null ? null : unit.sessionDate().toString()); result.put("series_key", unit.seriesKey()); result.put("window", unit.window()); return result; }
    private static List<WorkUnit> units(JsonObject root, String name) { List<WorkUnit> result = new ArrayList<>(); for (Object value : array(root, name)) { JsonObject item = object(value); fields(item, Set.of("unit_kind", "date_id", "ticker_id", "session_date", "series_key", "window")); result.add(new WorkUnit(text(item, "unit_kind"), longOrNull(item.get("date_id")), longOrNull(item.get("ticker_id")), item.get("session_date") == null ? null : LocalDate.parse(text(item, "session_date")), item.get("series_key") == null ? null : text(item, "series_key"), intOrNull(item.get("window")))); } return result; }
    private static Long longOrNull(Object value) { return value == null ? null : JsonSupport.longIntegerValue(value); }
    private static Integer intOrNull(Object value) { return value == null ? null : JsonSupport.integerValue(value); }
    private static int intValue(Object value) { if (!JsonSupport.isIntegralNumber(value) || !JsonSupport.fitsInt(value)) throw new ContractException("Expected integer"); return JsonSupport.integerValue(value); }
    private static JsonArray array(JsonObject root, String name) { return JsonSupport.requireArray(root.get(name), "Expected array: " + name); }
    private static JsonObject object(Object value) { return JsonSupport.requireObject(value, "Expected object"); }
    private static void fields(JsonObject value, Set<String> expected) { if (!new HashSet<>(value.keySet()).equals(expected)) throw new ContractException("Incomplete or unsupported contract fields"); }
    private static String text(JsonObject root, String name) { Object value = root.get(name); if (!JsonSupport.isString(value) || JsonSupport.stringValue(value).isEmpty()) throw new ContractException("Expected text: " + name); return JsonSupport.stringValue(value); }
    private static String text(Object value) { if (!JsonSupport.isString(value) || JsonSupport.stringValue(value).isEmpty()) throw new ContractException("Expected text"); return JsonSupport.stringValue(value); }
    private static void text(String value, String name) { if (value == null || value.isEmpty()) throw new ContractException("Expected text: " + name); }
    private static void sha(String value, String name) { text(value, name); if (!SHA256.matcher(value).matches()) throw new ContractException("Invalid SHA-256: " + name); }
    private static void id(long value, String name) { if (value <= 0) throw new ContractException("Invalid ID: " + name); }
    private static String instant(Instant value) { return value.toString(); }
    private static Instant parseInstant(String value) { if (!value.endsWith("Z")) throw new ContractException("Timestamp must be UTC"); return Instant.parse(value); }
    private static List<WorkUnit> copyUnique(List<WorkUnit> units, String name) { if (units == null) throw new ContractException("Missing " + name); List<WorkUnit> copy = List.copyOf(units); if (new HashSet<>(copy).size() != copy.size()) throw new ContractException("Duplicate " + name + " work unit"); return copy; }
}
