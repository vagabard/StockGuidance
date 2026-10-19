package stockguidance.validation;

import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import stockguidance.database.ConnectionFactory;
import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.ReadOnlyTransaction;
import stockguidance.database.SqlIdentifiers;
import stockguidance.database.TableNames;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

public final class StreamingValidator {
    private static final List<String> DERIVED_COLUMNS = LegacyDerivedCalculator.COLUMNS;
    private static final BigDecimal PRICE_LIMIT = new BigDecimal("1000000000000000");
    private static final BigDecimal PRICE_SCALE = new BigDecimal("0.0001");

    private final ConnectionFactory connectionFactory;
    private final TableNames configuredTableNames;

    public StreamingValidator(ConnectionFactory connectionFactory) {
        this(connectionFactory, null);
    }

    public StreamingValidator(ConnectionFactory connectionFactory, TableNames tableNames) {
        this.connectionFactory = connectionFactory;
        this.configuredTableNames = tableNames;
    }

    public ObjectNode validate(DatabaseConfiguration configuration, ValidationRequest request) {
        if (configuration == null || request == null) {
            throw new IllegalArgumentException("Validator requires explicit configuration and request");
        }
        TableNames tableNames = configuredTableNames == null ? configuration.loadTableNames() : configuredTableNames;
        return new ReadOnlyTransaction(connectionFactory, configuration).execute(connection -> inspect(connection, request, tableNames));
    }

    private ObjectNode inspect(Connection connection, ValidationRequest request, TableNames tableNames) throws SQLException {
        Inspection inspection = new Inspection(request);
        inspectSchema(connection, request, inspection, tableNames);
        if (!inspection.schemaUsable) {
            return report(request, inspection);
        }
        readDateMappings(connection, request, inspection, tableNames);
        readRaw(connection, request, inspection, tableNames);
        readDerived(connection, request, inspection, tableNames);
        readReceipts(connection, request, inspection, tableNames);
        reconcile(request, inspection);
        return report(request, inspection);
    }

    private static void inspectSchema(Connection connection, ValidationRequest request, Inspection inspection, TableNames tableNames) throws SQLException {
        List<String> tables = List.of(
                tableNames.source(), tableNames.sessions(), tableNames.tickers(), tableNames.prices(),
                tableNames.deltas(), tableNames.imports());
        String placeholders = placeholders(tables.size());
        try (PreparedStatement statement = connection.prepareStatement(
                "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN (" + placeholders + ") ORDER BY TABLE_NAME")) {
            inspection.sqlQueries++;
            int index = 1;
            for (String table : tables) {
                statement.setString(index++, table);
            }
            Set<String> found = new HashSet<>();
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    String table = rows.getString(1);
                    String engine = rows.getString(2);
                    if (table == null || engine == null || !found.add(table) || !engine.equals("InnoDB")) {
                        add(inspection, "unverified", "schema", "Updater tables, InnoDB engines, or source binding are missing or incompatible", null, List.of(), List.of());
                    }
                }
            }
            if (!found.equals(new HashSet<>(tables))) {
                add(inspection, "unverified", "schema", "Updater tables, InnoDB engines, or source binding are missing or incompatible", null, List.of(), List.of());
            }
        }
        try (PreparedStatement statement = connection.prepareStatement(
                "SELECT `source_key` FROM " + SqlIdentifiers.quote(tableNames.source(), "source table") + " WHERE `id` = 1")) {
            inspection.sqlQueries++;
            try (ResultSet rows = statement.executeQuery()) {
                if (!rows.next() || !request.sourceKey().equals(rows.getString(1)) || rows.next()) {
                    add(inspection, "unverified", "schema", "Database source binding does not match the explicit validation source", null, List.of(), List.of());
                }
            }
        }
        inspection.schemaUsable = inspection.findings.stream().noneMatch(item -> item.check().equals("schema"));
    }

    private static void readDateMappings(Connection connection, ValidationRequest request, Inspection inspection, TableNames tableNames) throws SQLException {
        List<LocalDate> dates = request.sessions().stream().map(ValidationRequest.SessionSpec::exchangeDate).toList();
        String placeholders = placeholders(dates.size());
        try (PreparedStatement statement = connection.prepareStatement(
                "SELECT `id`, `transaction_date` FROM " + SqlIdentifiers.quote(tableNames.sessions(), "sessions table") +
                        " WHERE `transaction_date` IN (" + placeholders + ") ORDER BY `transaction_date`, `id`")) {
            inspection.sqlQueries++;
            bindDates(statement, dates);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    Integer id = nullableInt(rows, 1);
                    LocalDate date = sqlDate(rows, 2);
                    if (id == null || date == null || !inspection.sessions.containsKey(date)) {
                        add(inspection, "invalid", "orphan", "Date mapping is outside the bounded authoritative calendar", date, List.of(), ids(id));
                        continue;
                    }
                    SessionData session = inspection.sessions.get(date);
                    if (session.dateId != null || inspection.dateIds.containsValue(id)) {
                        add(inspection, "invalid", "duplicate", "Duplicate date mapping", date, List.of(), ids(id));
                    } else {
                        session.dateId = id;
                        inspection.dateIds.put(date, id);
                    }
                }
            }
        }
    }

    private static void readRaw(Connection connection, ValidationRequest request, Inspection inspection, TableNames tableNames) throws SQLException {
        String placeholders = placeholders(request.sessions().size());
        String sql = "SELECT r.date_id, r.ticker_id, t.transaction_date, s.label, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions " +
                "FROM " + SqlIdentifiers.quote(tableNames.prices(), "prices table") + " r LEFT JOIN " + SqlIdentifiers.quote(tableNames.tickers(), "tickers table") + " s ON s.id = r.ticker_id LEFT JOIN " + SqlIdentifiers.quote(tableNames.sessions(), "sessions table") + " t ON t.id = r.date_id " +
                "WHERE t.transaction_date IN (" + placeholders + ") OR t.id IS NULL OR s.id IS NULL " +
                "ORDER BY t.transaction_date, r.date_id, r.ticker_id LIMIT ?";
        readRows(connection, request, inspection, sql, false, tableNames.prices());
    }

    private static void readDerived(Connection connection, ValidationRequest request, Inspection inspection, TableNames tableNames) throws SQLException {
        String placeholders = placeholders(request.sessions().size());
        String columns = String.join(", ", DERIVED_COLUMNS.stream().map(item -> "r." + item).toList());
        String sql = "SELECT r.date_id, r.ticker_id, t.transaction_date, s.label, " + columns + " " +
                "FROM " + SqlIdentifiers.quote(tableNames.deltas(), "deltas table") + " r LEFT JOIN " + SqlIdentifiers.quote(tableNames.tickers(), "tickers table") + " s ON s.id = r.ticker_id LEFT JOIN " + SqlIdentifiers.quote(tableNames.sessions(), "sessions table") + " t ON t.id = r.date_id " +
                "WHERE t.transaction_date IN (" + placeholders + ") OR t.id IS NULL OR s.id IS NULL " +
                "ORDER BY t.transaction_date, r.date_id, r.ticker_id LIMIT ?";
        readRows(connection, request, inspection, sql, true, tableNames.deltas());
    }

    private static void readRows(Connection connection, ValidationRequest request, Inspection inspection, String sql, boolean derived, String tableName) throws SQLException {
        List<LocalDate> dates = request.sessions().stream().map(ValidationRequest.SessionSpec::exchangeDate).toList();
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            inspection.sqlQueries++;
            bindDates(statement, dates);
            statement.setFetchSize(1000);
            statement.setInt(dates.size() + 1, request.maxRows() + 1);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    Integer dateId = nullableInt(rows, 1);
                    Integer tickerId = nullableInt(rows, 2);
                    LocalDate date = sqlDate(rows, 3);
                    inspection.rowCount(derived, date);
                    if (inspection.totalRows > request.maxRows()) {
                        inspection.bounded = false;
                        continue;
                    }
                    String ticker = nullableText(rows, 4);
                    if (dateId == null || tickerId == null || date == null || ticker == null || !inspection.dateIds.containsKey(date) || !dateId.equals(inspection.dateIds.get(date))) {
                        add(inspection, "invalid", "orphan", tableName + " record lacks a valid date or ticker mapping", date, ticker == null ? List.of() : List.of(ticker), ids(dateId, tickerId));
                        continue;
                    }
                    SessionData session = inspection.sessions.get(date);
                    checkTickerIdentity(inspection, tickerId, ticker, date);
                    if (derived) {
                        DerivedRowResult result = decodeDerived(rows, ticker);
                        if (!result.valid) {
                            add(inspection, "invalid", "legacy_derived", "Invalid " + tableName + " numerical values", date, List.of(ticker), ids(dateId, tickerId));
                            continue;
                        }
                        if (session.derived.putIfAbsent(ticker, result.row) != null) {
                            add(inspection, "invalid", "duplicate", "Duplicate derived source key", date, List.of(ticker), ids(dateId, tickerId));
                        }
                    } else {
                        RawBarResult result = decodeRaw(rows, ticker);
                        if (!result.valid) {
                            add(inspection, "invalid", "raw_values", "Invalid " + tableName + " prices, counts, or numerical values", date, List.of(ticker), ids(dateId, tickerId));
                            continue;
                        }
                        if (session.raw.putIfAbsent(ticker, result.bar) != null) {
                            add(inspection, "invalid", "duplicate", "Duplicate raw source key", date, List.of(ticker), ids(dateId, tickerId));
                        }
                    }
                }
            }
        }
    }

    private static void readReceipts(Connection connection, ValidationRequest request, Inspection inspection, TableNames tableNames) throws SQLException {
        String placeholders = placeholders(request.sessions().size());
        String sql = "SELECT `session_date`, `raw_id`, `available_at`, `derived_dependency_id`, `derived_content_id` FROM " +
                SqlIdentifiers.quote(tableNames.imports(), "imports table") + " WHERE `session_date` IN (" + placeholders + ") ORDER BY `session_date`";
        List<LocalDate> dates = request.sessions().stream().map(ValidationRequest.SessionSpec::exchangeDate).toList();
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            inspection.sqlQueries++;
            bindDates(statement, dates);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    inspection.receiptRows++;
                    LocalDate date = sqlDate(rows, 1);
                    if (date == null || !inspection.sessions.containsKey(date)) {
                        add(inspection, "invalid", "orphan", "Import receipt is outside the bounded authoritative calendar", date, List.of(), List.of());
                        continue;
                    }
                    SessionData session = inspection.sessions.get(date);
                    if (session.receipt != null) {
                        add(inspection, "invalid", "duplicate", "Duplicate import receipt", date, List.of(), List.of());
                        continue;
                    }
                    String rawId = nullableText(rows, 2);
                    String available = nullableText(rows, 3);
                    String dependency = nullableText(rows, 4);
                    String derivedId = nullableText(rows, 5);
                    Instant availableAt = parseInstant(available);
                    boolean pairValid = (dependency == null) == (derivedId == null);
                    if (rawId == null || !rawId.matches("[0-9a-f]{64}") || availableAt == null || !pairValid || (dependency != null && !dependency.matches("[0-9a-f]{64}")) || (derivedId != null && !derivedId.matches("[0-9a-f]{64}"))) {
                        add(inspection, "invalid", "import_receipt", "Invalid import receipt identity or availability", date, List.of(), List.of());
                        continue;
                    }
                    session.receipt = new Receipt(rawId, availableAt, dependency, derivedId);
                }
            }
        }
    }

    private static void reconcile(ValidationRequest request, Inspection inspection) {
        for (SessionData session : inspection.sessions.values()) {
            if (!session.requested) {
                continue;
            }
            ValidationRequest.SourceEvidence evidence = request.sourceEvidence().get(session.date);
            if (session.spec.closeAt().isAfter(request.asOf()) || (evidence != null && evidence.availableAt().isAfter(request.asOf())) || (session.receipt != null && session.receipt.availableAt.isAfter(request.asOf()))) {
                add(inspection, "not_yet_available", "session", "Daily observation or actual receipt is after the cutoff; values are not assessed", session.date, List.of(), List.of());
                continue;
            }
            if (evidence == null) {
                add(inspection, "unverified", "source_manifest", "Validated request-scoped source evidence is missing or corrupt", session.date, List.of(), List.of());
            } else if (!evidence.state().equals("complete") && !evidence.state().equals("no_trading")) {
                add(inspection, evidence.state().equals("not_yet_published") ? "not_yet_available" : "unverified", "source_manifest", "Provider count, continuation, or response evidence does not establish completeness", session.date, List.of(), List.of());
            } else if (evidence.recordCount() != evidence.keys().size()) {
                add(inspection, "unverified", "source_manifest", "Provider source count does not match the declared source keys", session.date, List.of(), List.of());
            }
            if (session.dateId == null) {
                add(inspection, "missing", "session", "Expected published session has no usable database mapping or records", session.date, List.of(), List.of());
                continue;
            }
            Set<String> rawKeys = new HashSet<>(session.raw.keySet());
            Set<String> derivedKeys = new HashSet<>(session.derived.keySet());
            if (evidence != null) {
                Set<String> expected = new HashSet<>(evidence.keys());
                Set<String> missing = difference(expected, rawKeys);
                Set<String> extra = difference(rawKeys, expected);
                if (!missing.isEmpty()) {
                    add(inspection, "missing", "raw_keys", "Source record keys are missing from raw storage", session.date, sorted(missing), List.of());
                }
                if (!extra.isEmpty()) {
                    add(inspection, "invalid", "raw_keys", "Raw storage contains keys absent from the declared provider response", session.date, sorted(extra), List.of());
                }
                if (!evidence.recordsId().equals(ValidationContent.rawId(session.raw))) {
                    add(inspection, "invalid", "raw_values", "Raw financial content differs from the validated provider response", session.date, sorted(rawKeys), List.of());
                }
            }
            if (session.receipt == null || !session.receipt.rawId.equals(ValidationContent.rawId(session.raw))) {
                add(inspection, "incomplete", "import_receipt", "Raw import receipt is absent or its revision differs from stored content", session.date, sorted(rawKeys), List.of());
            }
            Set<String> missingDerived = difference(rawKeys, derivedKeys);
            Set<String> orphanDerived = difference(derivedKeys, rawKeys);
            if (!missingDerived.isEmpty()) {
                add(inspection, "missing", "legacy_derived", "Required legacy derived rows are missing", session.date, sorted(missingDerived), List.of());
            }
            if (!orphanDerived.isEmpty()) {
                add(inspection, "invalid", "orphan", "Derived rows have no corresponding raw key", session.date, sorted(orphanDerived), List.of());
            }
            reconcileDerived(request, inspection, session);
        }
        if (hasUnresolvedHistory(request.history())) {
            add(inspection, "unverified", "history", "Request contains unresolved local/provider coverage limits", null, List.of(), List.of());
        }
    }

    private static void reconcileDerived(ValidationRequest request, Inspection inspection, SessionData session) {
        SessionData prior = inspection.bySequence.get(session.spec.sequence() - 1);
        SessionData following = inspection.bySequence.get(session.spec.sequence() + 1);
        Map<String, ValidationContent.RawBar> priorBars = verifiedRaw(prior);
        Map<String, ValidationContent.RawBar> currentBars = verifiedRaw(session);
        Map<String, ValidationContent.RawBar> followingBars = verifiedRaw(following);
        if (session.receipt == null || !session.receipt.rawId.equals(ValidationContent.rawId(session.raw))) {
            return;
        }
        String dependency = ValidationContent.dependencyId(rawId(priorBars), ValidationContent.rawId(session.raw), rawId(followingBars));
        if (session.receipt.derivedDependencyId == null || session.receipt.derivedContentId == null || !session.receipt.derivedDependencyId.equals(dependency) || !session.receipt.derivedContentId.equals(ValidationContent.derivedId(session.derived))) {
            add(inspection, "incomplete", "legacy_derived", "Derived import is interrupted or content/dependency receipts are stale", session.date, sorted(currentBars.keySet()), List.of());
        }
        Map<String, List<Double>> expected = expectedDerived(priorBars, currentBars, followingBars);
        for (Map.Entry<String, List<Double>> item : expected.entrySet()) {
            ValidationContent.DerivedRow actual = session.derived.get(item.getKey());
            if (actual == null) {
                continue;
            }
            for (int index = 0; index < DERIVED_COLUMNS.size(); index++) {
                Double observed = actual.values().get(index);
                Double required = item.getValue().get(index);
                if (!equal(observed, required)) {
                    add(inspection, observed == null ? "missing" : "invalid", index >= 8 ? "legacy_outcome" : "legacy_derived", "Legacy column " + DERIVED_COLUMNS.get(index) + " differs from available chronological dependencies", session.date, List.of(item.getKey()), List.of());
                }
            }
        }
    }

    private static Map<String, List<Double>> expectedDerived(Map<String, ValidationContent.RawBar> prior, Map<String, ValidationContent.RawBar> current, Map<String, ValidationContent.RawBar> following) {
        return LegacyDerivedCalculator.expected(prior, current, following);
    }

    private static Map<String, ValidationContent.RawBar> verifiedRaw(SessionData session) {
        return session == null || session.receipt == null || !session.receipt.rawId.equals(ValidationContent.rawId(session.raw)) ? Map.of() : session.raw;
    }

    private static String rawId(Map<String, ValidationContent.RawBar> bars) {
        return bars.isEmpty() ? ValidationContent.rawId(Map.of()) : ValidationContent.rawId(bars);
    }

    private static boolean equal(Double observed, Double required) {
        if (observed == null || required == null) {
            return observed == required;
        }
        return Double.isFinite(observed) && Math.abs(observed - required) <= Math.max(1e-12, 1e-10 * Math.max(Math.abs(observed), Math.abs(required)));
    }

    private static ObjectNode report(ValidationRequest request, Inspection inspection) {
        ObjectNode result = JsonNodeFactory.instance.objectNode();
        result.put("schema_version", 1);
        result.put("operation", request.operation());
        result.put("backend", "java");
        boolean failed = inspection.findings.stream().anyMatch(item -> item.category().equals("failed"));
        boolean complete = !failed && inspection.bounded && !inspection.findings.stream().anyMatch(ValidationFinding::blocking) && !inspection.requestedDates.isEmpty();
        result.put("complete", complete);
        result.put("failed", failed);
        result.put("exit_code", failed ? 1 : complete ? 0 : 2);
        result.set("source", request.source());
        ObjectNode calendar = result.putObject("calendar");
        calendar.put("id", request.calendarId());
        calendar.put("version", request.calendarVersion());
        ObjectNode scope = result.putObject("scope");
        scope.put("start", request.start().toString());
        scope.put("end", request.end().toString());
        scope.put("as_of", request.asOf().toString());
        result.set("history", request.history());
        ArrayNode unsupported = result.putArray("unsupported_checks");
        ArrayNode sessions = result.putArray("sessions");
        inspection.sessions.values().stream().filter(item -> item.requested).forEach(item -> appendSession(sessions, item, request, inspection));
        ArrayNode findings = result.putArray("findings");
        inspection.findings.stream().sorted(Comparator.comparing((ValidationFinding item) -> item.sessionDate() == null ? "" : item.sessionDate().toString()).thenComparing(ValidationFinding::check).thenComparing(ValidationFinding::detail)).forEach(item -> appendFinding(findings, item));
        result.set("repair_plan", repairPlan(request, inspection));
        ObjectNode metrics = result.putObject("metrics");
        metrics.put("sql_queries", inspection.sqlQueries);
        metrics.put("raw_rows", inspection.rawRows);
        metrics.put("derived_rows", inspection.derivedRows);
        metrics.put("receipt_rows", inspection.receiptRows);
        metrics.put("bounded", inspection.bounded);
        return result;
    }

    private static void appendSession(ArrayNode output, SessionData session, ValidationRequest request, Inspection inspection) {
        ObjectNode item = output.addObject();
        item.put("date", session.date.toString());
        item.put("state", sessionState(session.date, inspection));
        ArrayNode checks = item.putArray("checks");
        List<ValidationFinding> findings = inspection.findings.stream().filter(value -> session.date.equals(value.sessionDate())).toList();
        checks.add("session");
        if (!findings.stream().anyMatch(value -> value.check().equals("session") && value.category().equals("not_yet_available"))) {
            checks.add("source_manifest");
            checks.add("duplicate");
            checks.add("orphan");
            checks.add("raw_values");
            checks.add("import_receipt");
            checks.add("legacy_derived");
            checks.add("predecessor_history");
            if (request.sourceEvidence().containsKey(session.date)) {
                checks.add("raw_keys");
            }
        }
        ArrayNode sourceKeys = item.putArray("source_keys");
        ValidationRequest.SourceEvidence evidence = request.sourceEvidence().get(session.date);
        if (evidence != null && available(evidence.availableAt(), request.asOf())) {
            evidence.keys().stream().sorted().forEach(sourceKeys::add);
        }
        ArrayNode rawKeys = item.putArray("raw_valid_keys");
        if (available(session.receipt == null ? null : session.receipt.availableAt, request.asOf())) {
            session.raw.keySet().stream().sorted().forEach(rawKeys::add);
        }
        ArrayNode derivedKeys = item.putArray("derived_valid_keys");
        if (available(session.receipt == null ? null : session.receipt.availableAt, request.asOf())) {
            session.derived.keySet().stream().sorted().forEach(derivedKeys::add);
        }
        if (evidence == null || !available(evidence.availableAt(), request.asOf())) {
            item.putNull("source_count");
        } else {
            item.put("source_count", evidence.recordCount());
        }
        item.put("raw_count", available(session.receipt == null ? null : session.receipt.availableAt, request.asOf()) ? session.rawCount : 0);
        item.put("derived_count", available(session.receipt == null ? null : session.receipt.availableAt, request.asOf()) ? session.derivedCount : 0);
        item.put("source_state", evidence == null ? null : evidence.state());
        item.put("available_at", evidence == null ? null : evidence.availableAt().toString());
    }

    private static void appendFinding(ArrayNode output, ValidationFinding finding) {
        ObjectNode item = output.addObject();
        item.put("category", finding.category());
        item.put("check", finding.check());
        item.put("detail", finding.detail());
        if (finding.sessionDate() == null) item.putNull("session_date"); else item.put("session_date", finding.sessionDate().toString());
        if (finding.dependencySessionDate() == null) item.putNull("dependency_session_date"); else item.put("dependency_session_date", finding.dependencySessionDate().toString());
        ArrayNode tickers = item.putArray("tickers");
        finding.tickers().stream().sorted().forEach(tickers::add);
        ArrayNode ids = item.putArray("record_ids");
        finding.recordIds().stream().sorted().forEach(ids::add);
        item.put("blocking", finding.blocking());
    }

    private static ObjectNode repairPlan(ValidationRequest request, Inspection inspection) {
        ObjectNode plan = JsonNodeFactory.instance.objectNode();
        plan.put("schema_version", 1);
        plan.set("source", request.source());
        plan.put("start", request.start().toString());
        plan.put("end", request.end().toString());
        plan.put("as_of", request.asOf().toString());
        ArrayNode items = plan.putArray("items");
        Set<String> seen = new HashSet<>();
        for (ValidationFinding finding : inspection.findings) {
            if (!finding.blocking()) continue;
            String action = repairAction(finding.check());
            String key = action + "|" + finding.sessionDate() + "|" + finding.check() + "|" + finding.tickers();
            if (!seen.add(key)) continue;
            ObjectNode item = items.addObject();
            item.put("action", action);
            if (finding.sessionDate() == null) item.putNull("session_date"); else item.put("session_date", finding.sessionDate().toString());
            item.put("check", finding.check());
            ArrayNode tickers = item.putArray("tickers");
            finding.tickers().stream().sorted().forEach(tickers::add);
        }
        return plan;
    }

    private static String repairAction(String check) {
        return switch (check) {
            case "raw_keys", "raw_values", "import_receipt" -> "reimport";
            case "schema" -> "review_schema";
            case "duplicate", "orphan", "session", "source_manifest", "history" -> "review_records";
            default -> "rebuild_legacy_derived";
        };
    }

    private static String sessionState(LocalDate date, Inspection inspection) {
        List<String> categories = inspection.findings.stream().filter(item -> date.equals(item.sessionDate())).map(ValidationFinding::category).toList();
        for (String category : List.of("failed", "invalid", "unverified", "missing", "incomplete", "not_yet_available")) {
            if (categories.contains(category)) {
                return switch (category) {
                    case "failed", "invalid" -> "inconsistent";
                    default -> category;
                };
            }
        }
        return "complete";
    }

    private static boolean available(Instant value, Instant cutoff) {
        return value != null && !value.isAfter(cutoff);
    }

    private static boolean hasUnresolvedHistory(com.fasterxml.jackson.databind.JsonNode history) {
        return history.get("unresolved_limits") != null && history.get("unresolved_limits").isArray() && history.get("unresolved_limits").size() > 0;
    }

    private static Set<String> difference(Set<String> left, Set<String> right) {
        Set<String> result = new HashSet<>(left);
        result.removeAll(right);
        return result;
    }

    private static List<String> sorted(Set<String> values) {
        return values.stream().sorted().toList();
    }

    private static List<Integer> ids(Integer... values) {
        List<Integer> result = new ArrayList<>();
        for (Integer value : values) if (value != null) result.add(value);
        return result;
    }

    private static void add(Inspection inspection, String category, String check, String detail, LocalDate date, List<String> tickers, List<Integer> ids) {
        ValidationFinding finding = new ValidationFinding(category, check, detail, date, null, tickers, ids);
        String key = category + "|" + check + "|" + detail + "|" + date + "|" + tickers + "|" + ids;
        if (inspection.findingKeys.add(key)) inspection.findings.add(finding);
    }

    private static void checkTickerIdentity(Inspection inspection, Integer tickerId, String ticker, LocalDate date) {
        String knownTicker = inspection.tickerLabelsById.putIfAbsent(tickerId, ticker);
        Integer knownId = inspection.tickerIdsByLabel.putIfAbsent(ticker, tickerId);
        if ((knownTicker != null && !knownTicker.equals(ticker)) || (knownId != null && !knownId.equals(tickerId))) {
            add(inspection, "invalid", "duplicate", "Ambiguous ticker identity", date, List.of(ticker), List.of(tickerId));
        }
    }

    private static String placeholders(int size) {
        return String.join(",", java.util.Collections.nCopies(size, "?"));
    }

    private static void bindDates(PreparedStatement statement, List<LocalDate> dates) throws SQLException {
        for (int index = 0; index < dates.size(); index++) statement.setObject(index + 1, dates.get(index));
    }

    private static Integer nullableInt(ResultSet rows, int index) throws SQLException {
        int value = rows.getInt(index);
        return rows.wasNull() ? null : value;
    }

    private static Long nullableLong(ResultSet rows, int index) throws SQLException {
        long value = rows.getLong(index);
        return rows.wasNull() ? null : value;
    }

    private static BigDecimal nullableDecimal(ResultSet rows, int index) throws SQLException {
        BigDecimal value = rows.getBigDecimal(index);
        return rows.wasNull() ? null : value;
    }

    private static Double nullableDouble(ResultSet rows, int index) throws SQLException {
        double value = rows.getDouble(index);
        return rows.wasNull() ? null : value;
    }

    private static String nullableText(ResultSet rows, int index) throws SQLException {
        String value = rows.getString(index);
        return rows.wasNull() ? null : value;
    }

    private static LocalDate sqlDate(ResultSet rows, int index) throws SQLException {
        java.sql.Date value = rows.getDate(index);
        if (rows.wasNull() || value == null) return null;
        return value.toLocalDate();
    }

    private static Instant parseInstant(String value) {
        if (value == null) return null;
        try {
            return Instant.parse(value);
        } catch (RuntimeException error) {
            try {
                return OffsetDateTime.parse(value.replace(" ", "T")).toInstant();
            } catch (RuntimeException ignored) {
                return null;
            }
        }
    }

    private static RawBarResult decodeRaw(ResultSet rows, String ticker) throws SQLException {
        BigDecimal open = price(nullableDecimal(rows, 5));
        BigDecimal high = price(nullableDecimal(rows, 6));
        BigDecimal low = price(nullableDecimal(rows, 7));
        BigDecimal close = price(nullableDecimal(rows, 8));
        Long volume = nullableLong(rows, 9);
        BigDecimal vwap = optionalPrice(nullableDecimal(rows, 10));
        Long transactions = nullableLong(rows, 11);
        boolean valid = open != null && high != null && low != null && close != null && volume != null && volume >= 0 && (transactions == null || transactions >= 0) && (vwap == null || vwap.signum() > 0);
        if (valid) valid = low.compareTo(open.min(close)) <= 0 && high.compareTo(open.max(close)) >= 0 && low.compareTo(high) <= 0;
        return new RawBarResult(valid ? new ValidationContent.RawBar(ticker, open, high, low, close, volume, vwap, transactions) : null, valid);
    }

    private static DerivedRowResult decodeDerived(ResultSet rows, String ticker) throws SQLException {
        List<Double> values = new ArrayList<>();
        for (int index = 0; index < DERIVED_COLUMNS.size(); index++) {
            Double value = nullableDouble(rows, index + 5);
            if (value != null && !Double.isFinite(value)) return new DerivedRowResult(null, false);
            values.add(value);
        }
        return new DerivedRowResult(new ValidationContent.DerivedRow(ticker, values), true);
    }

    private static BigDecimal price(BigDecimal value) {
        if (value == null || !value.abs().equals(value) || value.signum() <= 0 || value.compareTo(PRICE_LIMIT) >= 0) return null;
        BigDecimal result = value.setScale(4, RoundingMode.HALF_UP);
        return result.signum() <= 0 ? null : result;
    }

    private static BigDecimal optionalPrice(BigDecimal value) {
        return value == null ? null : price(value);
    }

    private record RawBarResult(ValidationContent.RawBar bar, boolean valid) {
    }

    private record DerivedRowResult(ValidationContent.DerivedRow row, boolean valid) {
    }

    private record Receipt(String rawId, Instant availableAt, String derivedDependencyId, String derivedContentId) {
    }

    private static final class SessionData {
        private final LocalDate date;
        private final ValidationRequest.SessionSpec spec;
        private final boolean requested;
        private final Map<String, ValidationContent.RawBar> raw = new TreeMap<>();
        private final Map<String, ValidationContent.DerivedRow> derived = new TreeMap<>();
        private Integer dateId;
        private Receipt receipt;
        private int rawCount;
        private int derivedCount;

        private SessionData(ValidationRequest.SessionSpec spec, boolean requested) {
            this.date = spec.exchangeDate();
            this.spec = spec;
            this.requested = requested;
        }
    }

    private static final class Inspection {
        private final Map<LocalDate, SessionData> sessions = new TreeMap<>();
        private final Map<LocalDate, Integer> dateIds = new HashMap<>();
        private final Map<Integer, SessionData> bySequence = new HashMap<>();
        private final Map<Integer, String> tickerLabelsById = new HashMap<>();
        private final Map<String, Integer> tickerIdsByLabel = new HashMap<>();
        private final Set<LocalDate> requestedDates = new HashSet<>();
        private final List<ValidationFinding> findings = new ArrayList<>();
        private final Set<String> findingKeys = new HashSet<>();
        private boolean schemaUsable;
        private boolean bounded = true;
        private int totalRows;
        private int rawRows;
        private int derivedRows;
        private int receiptRows;
        private int sqlQueries;

        private Inspection(ValidationRequest request) {
            for (ValidationRequest.SessionSpec spec : request.sessions()) {
                boolean requested = !spec.exchangeDate().isBefore(request.start()) && !spec.exchangeDate().isAfter(request.end());
                SessionData session = new SessionData(spec, requested);
                sessions.put(spec.exchangeDate(), session);
                bySequence.put(spec.sequence(), session);
                if (requested) requestedDates.add(spec.exchangeDate());
            }
        }

        private void rowCount(boolean derived, LocalDate date) {
            totalRows++;
            if (derived) {
                derivedRows++;
                if (date != null && sessions.containsKey(date)) sessions.get(date).derivedCount++;
            } else {
                rawRows++;
                if (date != null && sessions.containsKey(date)) sessions.get(date).rawCount++;
            }
        }
    }
}
