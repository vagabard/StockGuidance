package stockguidance.validation;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.MySqlConnectionFactory;
import stockguidance.database.ReadOnlyTransaction;
import stockguidance.database.SqlIdentifiers;
import stockguidance.database.TableNames;
import stockguidance.json.CanonicalJson;
import stockguidance.json.JsonSupport;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.math.BigDecimal;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.Date;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.LocalDate;
import java.lang.management.ManagementFactory;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.Callable;
import java.util.concurrent.Executors;

public final class ArchiveStudyMain {
    private static final int DERIVED_BATCH_SIZE = 1;

    private ArchiveStudyMain() { }

    public static void main(String[] arguments) {
        try {
            if (arguments.length != 1) throw new IllegalArgumentException("Explicit request required");
            System.out.print(new String(CanonicalJson.bytes(run(JsonSupport.read(Path.of(arguments[0])))), StandardCharsets.UTF_8));
        } catch (Exception error) {
            System.out.print("{\"failed\":true,\"error\":\"Archive study failed\"}");
            System.exit(1);
        }
    }

    static ObjectNode run(JsonNode request) throws Exception {
        int workers = request.path("workers").asInt();
        int repeats = request.path("repeats").asInt(4);
        String mode = request.path("mode").asText();
        if (workers < 1 || workers > 3 || repeats < 1 || repeats > 10 || !List.of("files", "database", "reconcile", "derived").contains(mode)) {
            throw new IllegalArgumentException("Invalid study settings");
        }
        List<Path> paths = new ArrayList<>();
        for (JsonNode item : request.path("paths")) paths.add(Path.of(item.asText()));
        if (paths.isEmpty() || paths.stream().distinct().count() != paths.size()) throw new IllegalArgumentException("Distinct paths required");
        DatabaseConfiguration configuration = mode.equals("files") ? null : DatabaseConfiguration.load(Path.of(request.path("dbinfo_file").asText()));
        if (mode.equals("derived")) return runDerived(request, configuration);
        ObjectNode report = JsonNodeFactory.instance.objectNode();
        report.put("schema_version", 1).put("backend", "java").put("workers", workers).put("mode", mode);
        ArrayNode measurements = report.putArray("measurements");
        try (var executor = Executors.newFixedThreadPool(workers)) {
            for (int repeat = 0; repeat < repeats; repeat++) {
                long started = System.nanoTime();
                long cpuStarted = processCpu();
                List<Callable<List<ObjectNode>>> tasks = new ArrayList<>();
                for (int worker = 0; worker < workers; worker++) {
                    List<Path> chunk = new ArrayList<>();
                    for (int index = worker; index < paths.size(); index += workers) chunk.add(paths.get(index));
                    if (!chunk.isEmpty()) tasks.add(() -> configuration == null ? inspect(chunk, mode, null, null) :
                            new ReadOnlyTransaction(new MySqlConnectionFactory(), configuration).execute(connection -> inspect(chunk, mode, connection, configuration)));
                }
                List<ObjectNode> results = new ArrayList<>();
                for (var future : executor.invokeAll(tasks)) results.addAll(future.get());
                results.sort(Comparator.comparing(item -> item.path("session").asText()));
                ArrayNode sessions = JsonNodeFactory.instance.arrayNode();
                results.forEach(sessions::add);
                ObjectNode measurement = measurements.addObject();
                measurement.put("seconds", (System.nanoTime() - started) / 1e9);
                measurement.put("cpu_seconds", (processCpu() - cpuStarted) / 1e9);
                measurement.put("digest", CanonicalJson.sha256(sessions));
                measurement.set("sessions", sessions);
            }
        }
        return report;
    }

    private static ObjectNode runDerived(JsonNode request, DatabaseConfiguration configuration) throws Exception {
        int workers = request.path("workers").asInt();
        int repeats = request.path("repeats").asInt(4);
        List<String> paths = new ArrayList<>();
        for (JsonNode item : request.path("paths")) paths.add(item.asText());
        Map<String, List<String>> neighbors = neighbors(request, paths);
        ObjectNode report = JsonNodeFactory.instance.objectNode();
        report.put("schema_version", 1).put("backend", "java").put("workers", workers).put("mode", "derived");
        ArrayNode measurements = report.putArray("measurements");
        try (var executor = Executors.newFixedThreadPool(workers)) {
            for (int repeat = 0; repeat < repeats; repeat++) {
                long started = System.nanoTime();
                long cpuStarted = processCpu();
                List<Callable<DerivedChunk>> tasks = new ArrayList<>();
                for (int worker = 0; worker < workers; worker++) {
                    List<String> chunk = new ArrayList<>();
                    for (int index = worker; index < paths.size(); index += workers) chunk.add(paths.get(index));
                    if (!chunk.isEmpty()) {
                        tasks.add(() -> new ReadOnlyTransaction(new MySqlConnectionFactory(), configuration)
                                .execute(connection -> inspectDerived(chunk, neighbors, connection, configuration)));
                    }
                }
                List<ObjectNode> results = new ArrayList<>();
                int queryCount = 0;
                int dataQueryCount = 0;
                int rawRows = 0;
                int derivedRows = 0;
                for (var future : executor.invokeAll(tasks)) {
                    DerivedChunk chunk = future.get();
                    results.addAll(chunk.sessions());
                    queryCount += chunk.queryCount();
                    dataQueryCount += chunk.dataQueryCount();
                    rawRows += chunk.rawRows();
                    derivedRows += chunk.derivedRows();
                }
                results.sort(Comparator.comparing(item -> item.path("session").asText()));
                ArrayNode sessions = JsonNodeFactory.instance.arrayNode();
                results.forEach(sessions::add);
                ObjectNode measurement = measurements.addObject();
                measurement.put("seconds", (System.nanoTime() - started) / 1e9);
                measurement.put("cpu_seconds", (processCpu() - cpuStarted) / 1e9);
                measurement.put("query_count", queryCount);
                measurement.put("data_query_count", dataQueryCount);
                measurement.put("raw_rows", rawRows);
                measurement.put("derived_rows", derivedRows);
                measurement.put("digest", CanonicalJson.sha256(sessions));
                measurement.set("sessions", sessions);
            }
        }
        return report;
    }

    private static Map<String, List<String>> neighbors(JsonNode request, List<String> paths) {
        JsonNode value = request.get("neighbors");
        if (value == null || !value.isObject()) throw new IllegalArgumentException("Derived neighbors are required");
        Map<String, List<String>> result = new HashMap<>();
        var fields = value.fields();
        while (fields.hasNext()) {
            var item = fields.next();
            List<String> triple = new ArrayList<>();
            for (JsonNode path : item.getValue()) triple.add(path.asText());
            if (triple.size() != 3 || !triple.get(1).equals(item.getKey()) || triple.stream().distinct().count() != 3) {
                throw new IllegalArgumentException("Invalid derived neighbor triple");
            }
            result.put(item.getKey(), triple);
        }
        if (!result.keySet().equals(new HashSet<>(paths))) throw new IllegalArgumentException("Incomplete derived neighbor map");
        return result;
    }

    private static DerivedChunk inspectDerived(List<String> targetPaths, Map<String, List<String>> neighbors, Connection connection, DatabaseConfiguration configuration) throws SQLException {
        List<ObjectNode> sessions = new ArrayList<>();
        int queryCount = 0;
        int rawRows = 0;
        int derivedRows = 0;
        for (int start = 0; start < targetPaths.size(); start += DERIVED_BATCH_SIZE) {
            List<String> batch = targetPaths.subList(start, Math.min(start + DERIVED_BATCH_SIZE, targetPaths.size()));
            Set<LocalDate> dates = new HashSet<>();
            for (String path : batch) {
                for (String neighbor : neighbors.get(path)) dates.add(studyDate(neighbor));
            }
            DerivedScope scope = readDerivedScope(connection, configuration, dates);
            queryCount += scope.queryCount();
            rawRows += scope.rawRows();
            derivedRows += scope.derivedRows();
            for (String path : batch) {
                List<String> triple = neighbors.get(path);
                sessions.add(derivedResult(studyDate(triple.get(0)), studyDate(triple.get(1)), studyDate(triple.get(2)), scope.raw(), scope.derived()));
            }
        }
        return new DerivedChunk(sessions, queryCount + 2, queryCount, rawRows, derivedRows);
    }

    private static LocalDate studyDate(String path) {
        return LocalDate.parse(Path.of(path).getFileName().toString().replace(".ndjson", ""));
    }

    private static DerivedScope readDerivedScope(Connection connection, DatabaseConfiguration configuration, Set<LocalDate> requested) throws SQLException {
        TableNames names = configuration.loadTableNames();
        List<LocalDate> dates = requested.stream().sorted().toList();
        Map<Integer, LocalDate> dateById = new HashMap<>();
        int queryCount = 0;
        String datePlaceholders = placeholders(dates.size());
        try (PreparedStatement statement = connection.prepareStatement(
                "SELECT id, transaction_date FROM " + SqlIdentifiers.quote(names.sessions(), "sessions") + " WHERE transaction_date IN (" + datePlaceholders + ") ORDER BY transaction_date, id")) {
            queryCount++;
            bindDates(statement, dates);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    Integer id = nullableInt(rows, 1);
                    LocalDate date = sqlDate(rows, 2);
                    if (id == null || date == null || dateById.containsKey(id) || dateById.containsValue(date)) {
                        throw new IllegalArgumentException("Duplicate derived date mapping");
                    }
                    dateById.put(id, date);
                }
            }
        }
        if (dateById.isEmpty()) return new DerivedScope(new HashMap<>(), new HashMap<>(), queryCount, 0, 0);
        List<Integer> ids = dateById.keySet().stream().sorted().toList();
        String idPlaceholders = placeholders(ids.size());
        Map<LocalDate, Map<String, ValidationContent.RawBar>> raw = new HashMap<>();
        try (PreparedStatement statement = connection.prepareStatement(
                "SELECT r.date_id, s.label, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions FROM " +
                        SqlIdentifiers.quote(names.prices(), "prices") + " r JOIN " + SqlIdentifiers.quote(names.tickers(), "tickers") + " s ON s.id = r.ticker_id WHERE r.date_id IN (" + idPlaceholders + ") ORDER BY r.date_id, s.label")) {
            queryCount++;
            bindIds(statement, ids);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    Integer dateId = nullableInt(rows, 1);
                    String ticker = rows.getString(2);
                    LocalDate date = dateById.get(dateId);
                    if (date == null || ticker == null) throw new IllegalArgumentException("Invalid derived raw mapping");
                    ValidationContent.RawBar bar = decodeStudyRaw(rows, ticker);
                    if (raw.computeIfAbsent(date, ignored -> new TreeMap<>()).putIfAbsent(ticker, bar) != null) {
                        throw new IllegalArgumentException("Duplicate derived raw key");
                    }
                }
            }
        }
        Map<LocalDate, Map<String, ValidationContent.DerivedRow>> derived = new HashMap<>();
        String columns = String.join(", ", LegacyDerivedCalculator.COLUMNS.stream().map(item -> "r." + item).toList());
        try (PreparedStatement statement = connection.prepareStatement(
                "SELECT r.date_id, s.label, " + columns + " FROM " + SqlIdentifiers.quote(names.deltas(), "deltas") +
                        " r JOIN " + SqlIdentifiers.quote(names.tickers(), "tickers") + " s ON s.id = r.ticker_id WHERE r.date_id IN (" + idPlaceholders + ") ORDER BY r.date_id, s.label")) {
            queryCount++;
            bindIds(statement, ids);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    Integer dateId = nullableInt(rows, 1);
                    String ticker = rows.getString(2);
                    LocalDate date = dateById.get(dateId);
                    if (date == null || ticker == null) throw new IllegalArgumentException("Invalid derived row mapping");
                    List<Double> values = new ArrayList<>();
                    for (int index = 0; index < LegacyDerivedCalculator.COLUMNS.size(); index++) {
                        Double value = nullableDouble(rows, index + 3);
                        if (value != null && !Double.isFinite(value)) throw new IllegalArgumentException("Invalid derived numerical value");
                        values.add(value);
                    }
                    ValidationContent.DerivedRow row = new ValidationContent.DerivedRow(ticker, values);
                    if (derived.computeIfAbsent(date, ignored -> new TreeMap<>()).putIfAbsent(ticker, row) != null) {
                        throw new IllegalArgumentException("Duplicate derived key");
                    }
                }
            }
        }
        int rawRows = raw.values().stream().mapToInt(Map::size).sum();
        int derivedRows = derived.values().stream().mapToInt(Map::size).sum();
        return new DerivedScope(raw, derived, queryCount, rawRows, derivedRows);
    }

    private static ObjectNode derivedResult(LocalDate prior, LocalDate central, LocalDate following,
                                            Map<LocalDate, Map<String, ValidationContent.RawBar>> raw,
                                            Map<LocalDate, Map<String, ValidationContent.DerivedRow>> stored) {
        Map<String, ValidationContent.RawBar> previous = raw.getOrDefault(prior, Map.of());
        Map<String, ValidationContent.RawBar> current = raw.getOrDefault(central, Map.of());
        Map<String, ValidationContent.RawBar> next = raw.getOrDefault(following, Map.of());
        Map<String, ValidationContent.DerivedRow> actual = stored.getOrDefault(central, Map.of());
        ObjectNode result = JsonNodeFactory.instance.objectNode();
        result.put("session", central.toString());
        result.put("prior_session", prior.toString());
        result.put("following_session", following.toString());
        ObjectNode counts = result.putObject("raw_counts");
        counts.put("prior", previous.size()).put("current", current.size()).put("following", next.size());
        result.put("derived_count", actual.size());
        result.put("expected_count", 0);
        ArrayNode missing = result.putArray("missing_derived_keys");
        ArrayNode extra = result.putArray("extra_derived_keys");
        ObjectNode differences = result.putObject("column_difference_counts");
        LegacyDerivedCalculator.COLUMNS.forEach(column -> differences.put(column, 0));
        ObjectNode ids = result.putObject("raw_ids");
        if (previous.isEmpty()) ids.putNull("prior"); else ids.put("prior", ValidationContent.rawId(previous));
        if (current.isEmpty()) ids.putNull("current"); else ids.put("current", ValidationContent.rawId(current));
        if (next.isEmpty()) ids.putNull("following"); else ids.put("following", ValidationContent.rawId(next));
        result.putNull("expected_derived_id");
        result.putNull("stored_derived_id");
        result.put("state", "unavailable");
        result.put("reason", (current.isEmpty() || previous.isEmpty() || next.isEmpty()) ? "missing_raw_context" : null);
        result.put("matches", false);
        if (current.isEmpty() || previous.isEmpty() || next.isEmpty()) return result;
        Map<String, List<Double>> expected = LegacyDerivedCalculator.expected(previous, current, next);
        Set<String> missingKeys = new HashSet<>(expected.keySet());
        missingKeys.removeAll(actual.keySet());
        Set<String> extraKeys = new HashSet<>(actual.keySet());
        extraKeys.removeAll(expected.keySet());
        missingKeys.stream().sorted().forEach(missing::add);
        extraKeys.stream().sorted().forEach(extra::add);
        for (String ticker : expected.keySet()) {
            ValidationContent.DerivedRow row = actual.get(ticker);
            if (row == null) continue;
            for (int index : LegacyDerivedCalculator.differingColumns(row.values(), expected.get(ticker))) {
                String column = LegacyDerivedCalculator.COLUMNS.get(index);
                differences.put(column, differences.get(column).intValue() + 1);
            }
        }
        result.put("expected_count", expected.size());
        result.put("expected_derived_id", LegacyDerivedCalculator.studyId(expected));
        result.put("stored_derived_id", LegacyDerivedCalculator.studyId(LegacyDerivedCalculator.values(actual)));
        result.put("state", "complete");
        result.putNull("reason");
        boolean matches = missingKeys.isEmpty() && extraKeys.isEmpty();
        for (String column : LegacyDerivedCalculator.COLUMNS) matches &= differences.get(column).intValue() == 0;
        result.put("matches", matches);
        return result;
    }

    private static ValidationContent.RawBar decodeStudyRaw(ResultSet rows, String ticker) throws SQLException {
        BigDecimal open = price(nullableDecimal(rows, 3));
        BigDecimal high = price(nullableDecimal(rows, 4));
        BigDecimal low = price(nullableDecimal(rows, 5));
        BigDecimal close = price(nullableDecimal(rows, 6));
        Long volume = nullableLong(rows, 7);
        BigDecimal vwap = optionalPrice(nullableDecimal(rows, 8));
        Long transactions = nullableLong(rows, 9);
        if (open == null || high == null || low == null || close == null || volume == null || volume < 0 || (transactions != null && transactions < 0) || (vwap != null && vwap.signum() <= 0) || low.compareTo(open.min(close)) > 0 || high.compareTo(open.max(close)) < 0 || low.compareTo(high) > 0) {
            throw new IllegalArgumentException("Invalid derived raw value");
        }
        return new ValidationContent.RawBar(ticker, open, high, low, close, volume, vwap, transactions);
    }

    private static BigDecimal price(BigDecimal value) {
        if (value == null || value.signum() <= 0 || value.compareTo(new BigDecimal("1000000000000000")) >= 0) return null;
        BigDecimal result = value.setScale(4, java.math.RoundingMode.HALF_UP);
        return result.signum() <= 0 ? null : result;
    }

    private static BigDecimal optionalPrice(BigDecimal value) {
        return value == null ? null : price(value);
    }

    private static String placeholders(int size) {
        return String.join(",", java.util.Collections.nCopies(size, "?"));
    }

    private static void bindDates(PreparedStatement statement, List<LocalDate> dates) throws SQLException {
        for (int index = 0; index < dates.size(); index++) statement.setDate(index + 1, Date.valueOf(dates.get(index)));
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

    private static LocalDate sqlDate(ResultSet rows, int index) throws SQLException {
        Date value = rows.getDate(index);
        return rows.wasNull() || value == null ? null : value.toLocalDate();
    }

    private static void bindIds(PreparedStatement statement, List<Integer> ids) throws SQLException {
        for (int index = 0; index < ids.size(); index++) statement.setInt(index + 1, ids.get(index));
    }

    private record DerivedChunk(List<ObjectNode> sessions, int queryCount, int dataQueryCount, int rawRows, int derivedRows) {
    }

    private record DerivedScope(Map<LocalDate, Map<String, ValidationContent.RawBar>> raw,
                                Map<LocalDate, Map<String, ValidationContent.DerivedRow>> derived,
                                int queryCount, int rawRows, int derivedRows) {
    }

    private static long processCpu() {
        return ((com.sun.management.OperatingSystemMXBean) ManagementFactory.getOperatingSystemMXBean()).getProcessCpuTime();
    }

    private static List<ObjectNode> inspect(List<Path> paths, String mode, Connection connection, DatabaseConfiguration configuration) {
        try {
            List<ObjectNode> results = new ArrayList<>();
            for (Path path : paths) {
                LocalDate session = LocalDate.parse(path.getFileName().toString().replace(".ndjson", ""));
                ObjectNode result = JsonNodeFactory.instance.objectNode().put("session", session.toString());
                if (!mode.equals("database")) {
                    TreeMap<String, StudyBar> bars = new TreeMap<>();
                    MessageDigest digest = MessageDigest.getInstance("SHA-256");
                    try (var input = new java.security.DigestInputStream(Files.newInputStream(path), digest);
                         var reader = new java.io.BufferedReader(new java.io.InputStreamReader(input, StandardCharsets.UTF_8))) {
                        String line;
                        while ((line = reader.readLine()) != null) {
                            var bar = decode(JsonSupport.parse(line), session);
                            if (bars.putIfAbsent(bar.ticker(), bar) != null) throw new IllegalArgumentException("Duplicate archive ticker");
                        }
                    }
                    if (bars.isEmpty()) throw new IllegalArgumentException("Empty archive");
                    result.put("file_sha256", HexFormat.of().formatHex(digest.digest()));
                    result.put("archive_id", CanonicalJson.sha256(payload(new ArrayList<>(bars.values()))));
                    result.put("archive_count", bars.size());
                }
                if (!mode.equals("files")) {
                    List<StudyBar> bars = readDatabase(connection, configuration, session);
                    result.put("database_id", CanonicalJson.sha256(payload(bars)));
                    result.put("database_count", bars.size());
                }
                if (mode.equals("reconcile")) result.put("matches", result.path("archive_id").equals(result.path("database_id")));
                results.add(result);
            }
            return results;
        } catch (Exception error) {
            throw new IllegalStateException("Archive study inspection failed");
        }
    }

    private static List<StudyBar> readDatabase(Connection connection, DatabaseConfiguration configuration, LocalDate session) throws Exception {
        var names = configuration.loadTableNames();
        Long dateId = null;
        try (var statement = connection.prepareStatement("SELECT id FROM " + SqlIdentifiers.quote(names.sessions(), "sessions") + " WHERE transaction_date = ?")) {
            statement.setDate(1, Date.valueOf(session));
            try (var rows = statement.executeQuery()) {
                if (rows.next()) dateId = rows.getLong(1);
                if (rows.next()) throw new IllegalArgumentException("Duplicate session mapping");
            }
        }
        List<StudyBar> bars = new ArrayList<>();
        if (dateId == null) return bars;
        String sql = "SELECT s.label, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions FROM " +
                SqlIdentifiers.quote(names.prices(), "prices") + " r JOIN " + SqlIdentifiers.quote(names.tickers(), "tickers") + " s ON s.id = r.ticker_id WHERE r.date_id = ? ORDER BY s.label";
        try (var statement = connection.prepareStatement(sql, ResultSet.TYPE_FORWARD_ONLY, ResultSet.CONCUR_READ_ONLY)) {
            statement.setLong(1, dateId);
            statement.setFetchSize(1000);
            try (var rows = statement.executeQuery()) {
                while (rows.next()) {
                    ObjectNode row = JsonNodeFactory.instance.objectNode().put("T", rows.getString(1));
                    String[] prices = {"o", "h", "l", "c"};
                    for (int index = 0; index < prices.length; index++) row.put(prices[index], rows.getBigDecimal(index + 2));
                    row.put("v", rows.getBigDecimal(6));
                    BigDecimal vwap = rows.getBigDecimal(7);
                    if (vwap == null) row.putNull("vw"); else row.put("vw", vwap);
                    long transactions = rows.getLong(8);
                    if (rows.wasNull()) row.putNull("n"); else row.put("n", transactions);
                    row.put("t", session.atTime(12, 0).atZone(java.time.ZoneId.of("America/New_York")).toInstant().toEpochMilli());
                    bars.add(decode(row, session));
                }
            }
        }
        bars.sort(Comparator.comparing(StudyBar::ticker));
        if (bars.stream().map(StudyBar::ticker).distinct().count() != bars.size()) throw new IllegalArgumentException("Duplicate database ticker");
        return bars;
    }

    private record StudyBar(String ticker, BigDecimal open, BigDecimal high, BigDecimal low, BigDecimal close, BigDecimal volume, BigDecimal vwap, Long transactions) { }

    private static BigDecimal price(JsonNode row, String field) {
        JsonNode value = row.get(field);
        if (value == null || (!value.isNumber() && !value.isTextual())) throw new IllegalArgumentException("Invalid archive price");
        BigDecimal original = new BigDecimal(value.asText());
        BigDecimal result = original.setScale(4, java.math.RoundingMode.HALF_UP);
        if (original.signum() <= 0 || original.compareTo(new BigDecimal("1e60")) >= 0 || result.signum() <= 0) throw new IllegalArgumentException("Invalid archive price");
        return result;
    }

    private static StudyBar decode(JsonNode row, LocalDate session) {
        JsonNode value = row.get("v");
        if (value == null || !value.isNumber()) throw new IllegalArgumentException("Invalid archive volume");
        BigDecimal volume = value.decimalValue();
        if (volume.signum() < 0 || volume.compareTo(BigDecimal.valueOf(Long.MAX_VALUE)) > 0) throw new IllegalArgumentException("Invalid archive volume");
        JsonNode ticker = row.get("T");
        if (ticker == null || !ticker.isTextual() || ticker.asText().isEmpty() || ticker.asText().length() > 16 || ticker.asText().codePoints().anyMatch(Character::isWhitespace)) throw new IllegalArgumentException("Invalid archive ticker");
        JsonNode timestamp = row.get("t");
        if (timestamp == null || !timestamp.isIntegralNumber() || !timestamp.canConvertToLong() || timestamp.longValue() < 0 || !java.time.Instant.ofEpochMilli(timestamp.longValue()).atZone(java.time.ZoneId.of("America/New_York")).toLocalDate().equals(session)) throw new IllegalArgumentException("Invalid archive timestamp");
        JsonNode transactions = row.get("n");
        if (transactions != null && !transactions.isNull() && (!transactions.isIntegralNumber() || !transactions.canConvertToLong() || transactions.longValue() < 0)) throw new IllegalArgumentException("Invalid transactions");
        StudyBar bar = new StudyBar(ticker.asText(), price(row, "o"), price(row, "h"), price(row, "l"), price(row, "c"), volume, row.path("vw").isMissingNode() || row.path("vw").isNull() ? null : price(row, "vw"), transactions == null || transactions.isNull() ? null : transactions.longValue());
        if (bar.low().compareTo(bar.open().min(bar.close())) > 0 || bar.high().compareTo(bar.open().max(bar.close())) < 0 || bar.low().compareTo(bar.high()) > 0) throw new IllegalArgumentException("Inconsistent archive OHLC");
        return bar;
    }

    private static ArrayNode payload(List<StudyBar> bars) {
        ArrayNode result = JsonNodeFactory.instance.arrayNode();
        for (var item : bars) {
            var bar = item;
            ObjectNode row = result.addObject();
            row.put("T", bar.ticker()).put("o", bar.open().toPlainString()).put("h", bar.high().toPlainString()).put("l", bar.low().toPlainString()).put("c", bar.close().toPlainString()).put("v", item.volume().stripTrailingZeros().toPlainString());
            if (bar.vwap() == null) row.putNull("vw"); else row.put("vw", bar.vwap().toPlainString());
            if (bar.transactions() == null) row.putNull("n"); else row.put("n", bar.transactions());
        }
        return result;
    }
}
