package stockguidance.migration;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
import stockguidance.json.JsonSupport;


import stockguidance.database.SqlIdentifiers;
import stockguidance.database.TableNames;
import stockguidance.json.CanonicalJson;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Connection;
import java.sql.Date;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Types;
import java.time.LocalDate;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class JavaZScoreCandidate {
    private static final String CALCULATION_VERSION = "rolling-population-zscore-v1";
    private static final String PRECISION = "float32";
    private static final String DELTA_DEFINITION = "close_ratio_to_prior_minus_one";
    private static final String MISSING_VALUE_POLICY = "null_status_v1";
    private static final String TRIAL_RECEIPT = "2026-10-20T00:00:00+00:00";
    private static final double RELATIVE_TOLERANCE = 1e-10;
    private static final double ABSOLUTE_TOLERANCE = 1e-12;
    private static final int CHUNK_SIZE = 1000;

    private final JavaWriterManifest manifest;
    private final TableNames names;

    public JavaZScoreCandidate(JavaWriterManifest manifest) {
        this.manifest = manifest;
        this.names = manifest.tableNames();
    }

    public WriteResult write(Connection connection, LocalDate session, Family family, int window,
                             Path checkpoint, JavaWriterCandidate.InterruptPoint interruption) throws SQLException {
        validateWindow(window);
        JavaWriterManifest.TargetEntry target = manifest.target(session);
        List<LocalDate> inputDates = target.zscoreDependencies();
        validateInputDates(inputDates, session, window);
        verifySourceIdentities(inputDates);
        boolean committed = false;
        boolean originalAutoCommit = connection.getAutoCommit();
        try {
            connection.setAutoCommit(false);
            new JavaWriterCandidate(manifest).validateTarget(connection);
            Snapshot snapshot = loadSnapshot(connection, inputDates, session, family, window);
            validateCheckpoint(checkpoint, session, family, window, snapshot.contentDigest);
            String definitionKey = ensureSeries(connection, family, window);
            publish(connection, snapshot, definitionKey, session, family, window);
            JavaWriterCandidate.applyInterruption(interruption, JavaWriterCandidate.InterruptPoint.BEFORE_COMMIT);
            connection.commit();
            committed = true;
            JavaWriterCandidate.applyInterruption(interruption, JavaWriterCandidate.InterruptPoint.AFTER_COMMIT);
            writeCheckpoint(checkpoint, session, family, window, snapshot.contentDigest);
            return new WriteResult(family.value, window, session, "complete", true, snapshot.rows.size(),
                    snapshot.readyCount, snapshot.contentDigest, snapshot.dependencyDigest,
                    snapshot.availabilityDigest, snapshot.actualInputAvailable);
        } catch (RuntimeException | SQLException error) {
            if (!committed) rollback(connection);
            throw error;
        } finally {
            restoreAutoCommit(connection, originalAutoCommit);
        }
    }

    public static float quantizeFloat32(double value) {
        if (!Double.isFinite(value)) throw new IllegalArgumentException("Z-score is not finite");
        float result = (float) value;
        if (!Float.isFinite(result)) throw new IllegalArgumentException("Z-score is not representable as float32");
        return result;
    }

    static List<ScoreRow> calculate(Family family, int window, LocalDate target, List<LocalDate> dates,
                                    Map<Long, String> tickers, Map<Long, Map<LocalDate, Cell>> raw,
                                    Map<Long, Map<LocalDate, Cell>> deltas, Map<LocalDate, Evidence> evidence) {
        List<ScoreRow> result = new ArrayList<>();
        int targetIndex = dates.indexOf(target);
        if (targetIndex < 0) throw new IllegalArgumentException("Z-score target is outside its dependency window");
        for (long tickerId : tickers.keySet()) {
            List<Observation> observations = observations(family, tickerId, dates, raw, deltas, evidence);
            result.add(calculateTicker(family, window, tickerId, targetIndex, dates, observations));
        }
        result.sort(Comparator.comparingLong(ScoreRow::tickerId));
        return result;
    }

    private static ScoreRow calculateTicker(Family family, int window, long tickerId, int targetIndex,
                                            List<LocalDate> dates, List<Observation> observations) {
        ArrayDeque<NumberedValue> values = new ArrayDeque<>();
        ArrayDeque<IssueEntry> issues = new ArrayDeque<>();
        for (int index = 0; index < observations.size(); index++) {
            Observation observation = observations.get(index);
            issues.addLast(new IssueEntry(index, observation.issue));
            if (observation.issue == Issue.NONE) values.addLast(new NumberedValue(index, observation.value));
            int first = index - window + 1;
            while (!values.isEmpty() && values.peekFirst().sequence < first) values.removeFirst();
            while (!issues.isEmpty() && issues.peekFirst().sequence < first) issues.removeFirst();
            if (index != targetIndex) continue;
            Issue highest = highestIssue(issues);
            if (index + 1 < window) highest = higher(highest, Issue.INSUFFICIENT_HISTORY);
            if (highest != Issue.NONE) return new ScoreRow(tickerId, null, status(highest));
            if (values.size() != window) throw new IllegalStateException("Ready z-score window has the wrong size");
            double mean = compensatedMean(values);
            double variance = compensatedVariance(values, mean) / window;
            if (!Double.isFinite(variance) || variance < 0) throw new IllegalStateException("Invalid z-score variance");
            if (variance == 0.0) return new ScoreRow(tickerId, null, Status.ZERO_VARIANCE);
            double score = (observation.value - mean) / Math.sqrt(variance);
            return new ScoreRow(tickerId, quantizeFloat32(score), Status.READY);
        }
        throw new IllegalStateException("Z-score target was not calculated");
    }

    private static List<Observation> observations(Family family, long tickerId, List<LocalDate> dates,
                                                   Map<Long, Map<LocalDate, Cell>> raw,
                                                   Map<Long, Map<LocalDate, Cell>> deltas,
                                                   Map<LocalDate, Evidence> evidence) {
        List<Observation> result = new ArrayList<>();
        Map<LocalDate, Cell> tickerRaw = raw.getOrDefault(tickerId, Map.of());
        Map<LocalDate, Cell> tickerDeltas = deltas.getOrDefault(tickerId, Map.of());
        Observation previous = null;
        for (LocalDate date : dates) {
            Evidence source = evidence.get(date);
            Cell current = tickerRaw.get(date);
            Observation close = closeObservation(current, source);
            Observation value;
            if (family == Family.PRICE) {
                value = close;
            } else if (previous == null) {
                value = new Observation(Issue.INSUFFICIENT_HISTORY, null);
            } else {
                Cell stored = tickerDeltas.get(date);
                if (close.issue != Issue.NONE || previous.issue != Issue.NONE) {
                    value = new Observation(higher(higher(close.issue, previous.issue), Issue.NONE), null);
                } else if (stored == null || stored.value == null) {
                    value = new Observation(Issue.MISSING, null);
                } else if (!isFinite(stored.value) || stored.value <= 0.0
                        || !closeEnough(stored.value, current.value / previous.value)) {
                    value = new Observation(Issue.INVALID, null);
                } else {
                    value = new Observation(Issue.NONE, stored.value - 1.0);
                }
            }
            result.add(value);
            previous = close;
        }
        return result;
    }

    private static Observation closeObservation(Cell cell, Evidence evidence) {
        if (evidence == null || !evidence.verified()) return new Observation(Issue.UNVERIFIED, null);
        if (cell == null || cell.value == null) return new Observation(Issue.MISSING, null);
        if (!isFinite(cell.value) || cell.value <= 0.0) return new Observation(Issue.INVALID, null);
        return new Observation(Issue.NONE, cell.value);
    }

    private Snapshot loadSnapshot(Connection connection, List<LocalDate> dates, LocalDate target,
                                  Family family, int window) throws SQLException {
        Map<Long, String> tickerMappings = readTickerMappings(connection);
        Map<Long, String> expectedMappings = manifest.tickerMappings();
        for (Map.Entry<Long, String> expected : expectedMappings.entrySet()) {
            if (!expected.getValue().equals(tickerMappings.get(expected.getKey()))) {
                throw new IllegalArgumentException("Ticker mapping differs from manifest");
            }
        }
        tickerMappings = expectedMappings;
        Map<LocalDate, Long> dateIds = readDateMappings(connection, dates);
        Map<LocalDate, Evidence> evidence = readEvidence(connection, dates);
        MessageDigest dependencyDigest = digest();
        MessageDigest availabilityDigest = digest();
        List<Long> tickerIds = new ArrayList<>(tickerMappings.keySet());
        Collections.sort(tickerIds);
        List<ScoreRow> rows = new ArrayList<>();
        boolean actualAvailable = true;
        for (int offset = 0; offset < tickerIds.size(); offset += CHUNK_SIZE) {
            List<Long> chunk = tickerIds.subList(offset, Math.min(offset + CHUNK_SIZE, tickerIds.size()));
            Map<Long, Map<LocalDate, Cell>> rawChunk = readValues(connection, names.prices(), "close", dateIds, chunk);
            Map<Long, Map<LocalDate, Cell>> deltaChunk = family == Family.PRICE_DELTA
                    ? readValues(connection, names.deltas(), "close_ratio_to_prior", dateIds, chunk) : Map.of();
            for (long tickerId : chunk) {
                Map<LocalDate, Cell> tickerRaw = rawChunk.getOrDefault(tickerId, Map.of());
                Map<LocalDate, Cell> tickerDelta = deltaChunk.getOrDefault(tickerId, Map.of());
                for (LocalDate date : dates) {
                    Evidence item = evidence.get(date);
                    updateDigest(dependencyDigest, date, dateIds.get(date), tickerId, tickerRaw.get(date),
                            family == Family.PRICE_DELTA ? tickerDelta.get(date) : null, item);
                    Observation close = closeObservation(tickerRaw.get(date), item);
                    updateDigest(availabilityDigest, date, tickerId, close.issue.name().toLowerCase());
                    if (close.issue != Issue.NONE) actualAvailable = false;
                }
            }
            rows.addAll(calculate(family, window, target, dates, tickerMappingsFor(chunk, tickerMappings), rawChunk, deltaChunk, evidence));
        }
        rows.sort(Comparator.comparingLong(ScoreRow::tickerId));
        String dependency = hex(dependencyDigest.digest());
        String availability = hex(availabilityDigest.digest());
        int ready = (int) rows.stream().filter(row -> row.status == Status.READY).count();
        String content = rowsContentId(rows, target);
        return new Snapshot(rows, ready, dependency, availability, actualAvailable, content, dateIds.get(target));
    }

    private static Map<Long, String> tickerMappingsFor(List<Long> ids, Map<Long, String> mappings) {
        Map<Long, String> result = new LinkedHashMap<>();
        for (long id : ids) result.put(id, mappings.get(id));
        return result;
    }

    private Map<Long, String> readTickerMappings(Connection connection) throws SQLException {
        Map<Long, String> result = new LinkedHashMap<>();
        String sql = "SELECT id, label FROM " + SqlIdentifiers.quote(names.tickers(), "tickers") + " ORDER BY id FOR UPDATE";
        try (PreparedStatement statement = connection.prepareStatement(sql); ResultSet rows = statement.executeQuery()) {
            while (rows.next()) {
                long id = rows.getLong(1);
                String label = rows.getString(2);
                if (id < 1 || label == null || result.putIfAbsent(id, label) != null) throw new IllegalArgumentException("Invalid ticker mapping");
            }
        }
        return result;
    }

    private Map<LocalDate, Long> readDateMappings(Connection connection, List<LocalDate> dates) throws SQLException {
        Map<LocalDate, Long> result = new HashMap<>();
        String sql = "SELECT id, transaction_date FROM " + SqlIdentifiers.quote(names.sessions(), "sessions")
                + " WHERE transaction_date IN (" + placeholders(dates.size()) + ") ORDER BY transaction_date, id FOR UPDATE";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            for (int index = 0; index < dates.size(); index++) statement.setDate(index + 1, Date.valueOf(dates.get(index)));
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    LocalDate date = rows.getDate(2).toLocalDate();
                    if (result.putIfAbsent(date, rows.getLong(1)) != null) throw new IllegalArgumentException("Duplicate date mapping");
                }
            }
        }
        return result;
    }

    private Map<LocalDate, Evidence> readEvidence(Connection connection, List<LocalDate> dates) throws SQLException {
        Map<LocalDate, Evidence> result = new LinkedHashMap<>();
        for (LocalDate date : dates) {
            JavaWriterManifest.FileEntry file = manifest.file(date);
            result.put(date, new Evidence(file.valid() && file.recordsId() != null, file.recordsId(), null, null));
        }
        String sql = "SELECT session_date, raw_id, available_at FROM " + SqlIdentifiers.quote(names.imports(), "imports")
                + " WHERE session_date IN (" + placeholders(dates.size()) + ") ORDER BY session_date FOR UPDATE";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            for (int index = 0; index < dates.size(); index++) statement.setDate(index + 1, Date.valueOf(dates.get(index)));
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    LocalDate date = rows.getDate(1).toLocalDate();
                    Evidence previous = result.get(date);
                    if (previous == null || previous.rawId != null) throw new IllegalArgumentException("Duplicate or out-of-scope import evidence");
                    String rawId = rows.getString(2);
                    String availableAt = rows.getString(3);
                    result.put(date, new Evidence(previous.expected, previous.expectedId, rawId, availableAt));
                }
            }
        }
        for (Map.Entry<LocalDate, Evidence> entry : result.entrySet()) {
            Evidence value = entry.getValue();
            boolean verified = value.expected && value.rawId != null && value.rawId.equals(value.expectedId)
                    && value.availableAt != null && !value.availableAt.isBlank();
            entry.setValue(new Evidence(verified, value.expectedId, value.rawId, value.availableAt));
        }
        return result;
    }

    private Map<Long, Map<LocalDate, Cell>> readValues(Connection connection, String table, String column,
                                                       Map<LocalDate, Long> dateIds, List<Long> tickerIds) throws SQLException {
        Map<Long, Map<LocalDate, Cell>> result = new HashMap<>();
        List<Map.Entry<LocalDate, Long>> dates = new ArrayList<>(dateIds.entrySet());
        String sql = "SELECT date_id, ticker_id, " + SqlIdentifiers.quote(column, column) + " FROM "
                + SqlIdentifiers.quote(table, table) + " WHERE date_id IN (" + placeholders(dates.size()) + ")"
                + " AND ticker_id IN (" + placeholders(tickerIds.size()) + ") ORDER BY ticker_id, date_id FOR UPDATE";
        try (PreparedStatement statement = connection.prepareStatement(sql, ResultSet.TYPE_FORWARD_ONLY, ResultSet.CONCUR_READ_ONLY)) {
            int index = 1;
            for (Map.Entry<LocalDate, Long> date : dates) statement.setLong(index++, date.getValue());
            for (long tickerId : tickerIds) statement.setLong(index++, tickerId);
            statement.setFetchSize(1000);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    long dateId = rows.getLong(1);
                    long tickerId = rows.getLong(2);
                    LocalDate date = dates.stream().filter(item -> item.getValue() == dateId).findFirst().orElseThrow().getKey();
                    Map<LocalDate, Cell> ticker = result.computeIfAbsent(tickerId, ignored -> new HashMap<>());
                    BigDecimal value = rows.getBigDecimal(3);
                    if (ticker.put(date, new Cell(value == null ? null : value.doubleValue())) != null) throw new IllegalArgumentException("Duplicate z-score input row");
                }
            }
        }
        return result;
    }

    private String ensureSeries(Connection connection, Family family, int window) throws SQLException {
        JsonObject definition = definition(family, window);
        String key = CanonicalJson.sha256(definition);
        String table = SqlIdentifiers.quote(names.zscoreSeries(), "z-score series");
        String select = "SELECT schema_version, source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy FROM " + table + " WHERE series_key = ? FOR UPDATE";
        try (PreparedStatement statement = connection.prepareStatement(select)) {
            statement.setString(1, key);
            try (ResultSet rows = statement.executeQuery()) {
                if (rows.next()) {
                    if (rows.getInt(1) != 1 || !manifest.sourceScope().equals(rows.getString(2))
                            || !manifest.calendarId().equals(rows.getString(3)) || !family.value.equals(rows.getString(4))
                            || rows.getInt(5) != window || !CALCULATION_VERSION.equals(rows.getString(6))
                            || !deltaDefinition(family).equals(rows.getString(7)) || !PRECISION.equals(rows.getString(8))
                            || !MISSING_VALUE_POLICY.equals(rows.getString(9))) throw new IllegalArgumentException("Conflicting z-score series metadata");
                    if (rows.next()) throw new IllegalArgumentException("Duplicate z-score series metadata");
                    return key;
                }
            }
        }
        String insert = "INSERT INTO " + table + " (series_key, schema_version, source_scope, calendar_id, family, `window`, calculation_version, delta_definition, `precision`, missing_value_policy, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)";
        try (PreparedStatement statement = connection.prepareStatement(insert)) {
            statement.setString(1, key); statement.setInt(2, 1); statement.setString(3, manifest.sourceScope());
            statement.setString(4, manifest.calendarId()); statement.setString(5, family.value); statement.setInt(6, window);
            statement.setString(7, CALCULATION_VERSION); statement.setString(8, deltaDefinition(family));
            statement.setString(9, PRECISION); statement.setString(10, MISSING_VALUE_POLICY); statement.setString(11, TRIAL_RECEIPT);
            statement.executeUpdate();
        }
        return key;
    }

    private void publish(Connection connection, Snapshot snapshot, String seriesKey, LocalDate target,
                         Family family, int window) throws SQLException {
        if (snapshot.dateId == null) throw new IllegalArgumentException("Z-score target date mapping is missing");
        String physical = names.resolveZScoreTables(List.of(window)).get(window).price();
        if (family == Family.PRICE_DELTA) physical = names.resolveZScoreTables(List.of(window)).get(window).priceDelta();
        String table = SqlIdentifiers.quote(physical, "z-score rows");
        try (PreparedStatement delete = connection.prepareStatement("DELETE FROM " + table + " WHERE date_id = ?")) {
            delete.setLong(1, snapshot.dateId); delete.executeUpdate();
        }
        try (PreparedStatement insert = connection.prepareStatement("INSERT INTO " + table + " (date_id, ticker_id, zscore, status) VALUES (?,?,?,?)")) {
            for (ScoreRow row : snapshot.rows) {
                insert.setLong(1, snapshot.dateId); insert.setLong(2, row.tickerId);
                if (row.score == null) insert.setNull(3, Types.FLOAT); else insert.setFloat(3, row.score);
                insert.setInt(4, row.status.code);
                insert.addBatch();
            }
            insert.executeBatch();
        }
        String metadata = SqlIdentifiers.quote(names.zscoreSessions(), "z-score sessions");
        try (PreparedStatement delete = connection.prepareStatement("DELETE FROM " + metadata + " WHERE series_key = ? AND date_id = ?")) {
            delete.setString(1, seriesKey); delete.setLong(2, snapshot.dateId); delete.executeUpdate();
        }
        int[] counts = new int[6];
        for (ScoreRow row : snapshot.rows) counts[row.status.code]++;
        String serialized = String.format("0:%d,1:%d,2:%d,3:%d,4:%d,5:%d", counts[0], counts[1], counts[2], counts[3], counts[4], counts[5]);
        try (PreparedStatement insert = connection.prepareStatement("INSERT INTO " + metadata + " (series_key, date_id, dependency_digest, availability_digest, actual_input_available, completed_at, row_count, ready_count, status_counts, content_digest) VALUES (?,?,?,?,?,?,?,?,?,?)")) {
            insert.setString(1, seriesKey); insert.setLong(2, snapshot.dateId); insert.setString(3, snapshot.dependencyDigest);
            insert.setString(4, snapshot.availabilityDigest); insert.setBoolean(5, snapshot.actualInputAvailable);
            insert.setString(6, TRIAL_RECEIPT); insert.setInt(7, snapshot.rows.size()); insert.setInt(8, snapshot.readyCount);
            insert.setString(9, serialized); insert.setString(10, snapshot.contentDigest); insert.executeUpdate();
        }
    }

    private JsonObject definition(Family family, int window) {
        JsonObject value = JsonSupport.object();
        value.put("schema_version", 1); value.put("source_scope", manifest.sourceScope()); value.put("calendar_id", manifest.calendarId());
        value.put("family", family.value); value.put("window", window); value.put("calculation_version", CALCULATION_VERSION);
        value.put("delta_definition", deltaDefinition(family)); value.put("precision", PRECISION); value.put("missing_value_policy", MISSING_VALUE_POLICY);
        return value;
    }

    private static String rowsContentId(List<ScoreRow> rows, LocalDate session) {
        JsonArray values = JsonSupport.array();
        for (ScoreRow row : rows) {
            JsonArray item = JsonSupport.addArray(values); item.add(session.toString()); item.add(row.tickerId);
            item.add(row.score);
            item.add(row.status.code);
        }
        return CanonicalJson.sha256(values);
    }

    private static void updateDigest(MessageDigest digest, LocalDate date, Long dateId, long tickerId, Cell raw, Cell delta, Evidence evidence) {
        updateDigest(digest, date + "|" + dateId + "|" + tickerId + "|" + decimal(raw) + "|" + decimal(delta) + "|"
                + (evidence == null ? "null" : evidence.rawId) + "|" + (evidence == null ? "null" : evidence.availableAt));
    }

    private static void updateDigest(MessageDigest digest, LocalDate date, long tickerId, String state) {
        updateDigest(digest, date + "|" + tickerId + "|" + state);
    }

    private static String decimal(Cell cell) {
        return cell == null || cell.value == null ? "null" : BigDecimal.valueOf(cell.value).stripTrailingZeros().toPlainString();
    }

    private static void updateDigest(MessageDigest digest, String value) {
        digest.update(value.getBytes(StandardCharsets.UTF_8)); digest.update((byte) '\n');
    }

    private void verifySourceIdentities(List<LocalDate> dates) {
        for (LocalDate date : dates) {
            if (manifest.isAdoptable(date)) manifest.archive(date);
        }
    }

    private static double compensatedMean(ArrayDeque<NumberedValue> values) {
        double sum = 0.0, correction = 0.0;
        for (NumberedValue item : values) {
            double next = sum + item.value;
            correction += Math.abs(sum) >= Math.abs(item.value) ? (sum - next) + item.value : (item.value - next) + sum;
            sum = next;
        }
        return (sum + correction) / values.size();
    }

    private static double compensatedVariance(ArrayDeque<NumberedValue> values, double mean) {
        double sum = 0.0, correction = 0.0;
        for (NumberedValue item : values) {
            double squared = (item.value - mean) * (item.value - mean);
            double next = sum + squared;
            correction += Math.abs(sum) >= Math.abs(squared) ? (sum - next) + squared : (squared - next) + sum;
            sum = next;
        }
        return sum + correction;
    }

    private static Issue highestIssue(ArrayDeque<IssueEntry> issues) {
        Issue result = Issue.NONE;
        for (IssueEntry value : issues) result = higher(result, value.issue);
        return result;
    }

    private static Issue higher(Issue first, Issue second) {
        return first.rank >= second.rank ? first : second;
    }

    private static Status status(Issue issue) {
        return switch (issue) {
            case INSUFFICIENT_HISTORY -> Status.INSUFFICIENT_HISTORY;
            case MISSING -> Status.MISSING_INPUT;
            case UNVERIFIED -> Status.UNVERIFIED_INPUT;
            case INVALID -> Status.INVALID_INPUT;
            case NONE -> throw new IllegalArgumentException("Ready input has no unavailable status");
        };
    }

    private static boolean closeEnough(double actual, double expected) {
        return Math.abs(actual - expected) <= Math.max(ABSOLUTE_TOLERANCE, RELATIVE_TOLERANCE * Math.abs(expected));
    }

    private static boolean isFinite(Double value) {
        return value != null && Double.isFinite(value);
    }

    private static String deltaDefinition(Family family) {
        return family == Family.PRICE_DELTA ? DELTA_DEFINITION : "not_applicable";
    }

    private static void validateInputDates(List<LocalDate> dates, LocalDate target, int window) {
        if (dates == null || dates.isEmpty() || !dates.contains(target)) throw new IllegalArgumentException("Z-score dependencies are incomplete");
        Set<LocalDate> seen = new HashSet<>();
        LocalDate previous = null;
        for (LocalDate date : dates) {
            if (!seen.add(date) || (previous != null && !previous.isBefore(date))) throw new IllegalArgumentException("Z-score dependencies are not ordered");
            previous = date;
        }
        if (dates.indexOf(target) + 1 < window) throw new IllegalArgumentException("Z-score target lacks its declared window context");
    }

    private static void validateWindow(int window) {
        if (window < 2) throw new IllegalArgumentException("Z-score window must be at least two");
    }

    private static String placeholders(int count) {
        if (count < 1) throw new IllegalArgumentException("Bounded query requires parameters");
        return String.join(",", Collections.nCopies(count, "?"));
    }

    private static MessageDigest digest() {
        try {
            return MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-256 is unavailable", error);
        }
    }

    private static String hex(byte[] bytes) {
        return java.util.HexFormat.of().formatHex(bytes);
    }

    private void validateCheckpoint(Path checkpoint, LocalDate session, Family family, int window, String digest) {
        if (checkpoint == null || !Files.exists(checkpoint)) return;
        try {
            JsonObject value = JsonSupport.read(checkpoint);
            if (JsonSupport.integerOrDefault(value.get("schema_version"), -1) != 1
                    || !manifest.manifestId().equals(JsonSupport.stringOrEmpty(value.get("manifest_id")))
                    || !"zscores".equals(JsonSupport.stringOrEmpty(value.get("workload")))
                    || !family.value.equals(JsonSupport.stringOrEmpty(value.get("family"))) || JsonSupport.integerOrDefault(value.get("window"), -1) != window
                    || !session.toString().equals(JsonSupport.stringOrEmpty(value.get("session")))
                    || !digest.equals(JsonSupport.stringOrEmpty(value.get("content_digest"))) || !JsonSupport.booleanOrDefault(value.get("after_commit"), false)) {
                throw new IllegalArgumentException("Stale or incompatible Java z-score checkpoint");
            }
        } catch (RuntimeException error) {
            if (error.getMessage() != null && error.getMessage().startsWith("Stale or incompatible")) throw error;
            throw new IllegalArgumentException("Stale or incompatible Java z-score checkpoint", error);
        }
    }

    private void writeCheckpoint(Path checkpoint, LocalDate session, Family family, int window, String digest) {
        if (checkpoint == null) return;
        JsonObject value = JsonSupport.object(); value.put("schema_version", 1);
        value.put("manifest_id", manifest.manifestId()); value.put("workload", "zscores"); value.put("family", family.value);
        value.put("window", window); value.put("session", session.toString()); value.put("content_digest", digest);
        value.put("after_commit", true); value.put("committed_at", TRIAL_RECEIPT);
        try {
            Path parent = checkpoint.toAbsolutePath().normalize().getParent();
            if (parent != null) Files.createDirectories(parent);
            Files.write(checkpoint, CanonicalJson.bytes(value));
        } catch (Exception error) {
            throw new IllegalArgumentException("Cannot write Java z-score checkpoint", error);
        }
    }

    private static void rollback(Connection connection) {
        try { connection.rollback(); } catch (SQLException ignored) { }
    }

    private static void restoreAutoCommit(Connection connection, boolean original) {
        try { connection.setAutoCommit(original); } catch (SQLException ignored) { }
    }

    public enum Family {
        PRICE("price"), PRICE_DELTA("price_delta");

        private final String value;

        Family(String value) { this.value = value; }

        public static Family parse(String value) {
            for (Family family : values()) if (family.value.equals(value)) return family;
            throw new IllegalArgumentException("Unsupported z-score family");
        }
    }

    public enum Status {
        READY(0), INSUFFICIENT_HISTORY(1), MISSING_INPUT(2), UNVERIFIED_INPUT(3), INVALID_INPUT(4), ZERO_VARIANCE(5);

        private final int code;

        Status(int code) { this.code = code; }
    }

    private enum Issue {
        NONE(0), INSUFFICIENT_HISTORY(1), MISSING(2), UNVERIFIED(3), INVALID(4);

        private final int rank;

        Issue(int rank) { this.rank = rank; }
    }

    static record Cell(Double value) { }

    static record Evidence(boolean expected, String expectedId, String rawId, String availableAt) {
        private boolean verified() { return expected && expectedId != null && expectedId.equals(rawId) && availableAt != null && !availableAt.isBlank(); }
    }

    private record Observation(Issue issue, Double value) { }

    private record NumberedValue(int sequence, double value) { }

    private record IssueEntry(int sequence, Issue issue) { }

    static record ScoreRow(long tickerId, Float score, Status status) { }

    private record Snapshot(List<ScoreRow> rows, int readyCount, String dependencyDigest, String availabilityDigest,
                            boolean actualInputAvailable, String contentDigest, Long dateId) { }

    public record WriteResult(String family, int window, LocalDate session, String status, boolean committed,
                              int rowCount, int readyCount, String contentDigest, String dependencyDigest,
                              String availabilityDigest, boolean actualInputAvailable) { }
}
