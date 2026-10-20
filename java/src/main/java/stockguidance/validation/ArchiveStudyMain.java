package stockguidance.validation;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
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

    static JsonObject run(JsonObject request) throws Exception {
        int workers = JsonSupport.integerOrDefault(request.get("workers"));
        int repeats = JsonSupport.integerOrDefault(request.get("repeats"), 4);
        String mode = JsonSupport.stringOrEmpty(request.get("mode"));
        if (workers < 1 || workers > 3 || repeats < 1 || repeats > 10 || !List.of("files", "database", "reconcile", "derived").contains(mode)) {
            throw new IllegalArgumentException("Invalid study settings");
        }
        List<Path> paths = new ArrayList<>();
        JsonArray pathValues = JsonSupport.requireArray(request.get("paths"), "Study paths are required");
        for (Object item : pathValues) paths.add(Path.of(JsonSupport.stringOrEmpty(item)));
        if (paths.isEmpty() || paths.stream().distinct().count() != paths.size()) throw new IllegalArgumentException("Distinct paths required");
        DatabaseConfiguration configuration = mode.equals("files") ? null : DatabaseConfiguration.load(Path.of(JsonSupport.stringOrEmpty(request.get("dbinfo_file"))));
        if (mode.equals("derived")) return runDerived(request, configuration);
        JsonObject report = JsonSupport.object();
        report.put("schema_version", 1); report.put("backend", "java"); report.put("workers", workers); report.put("mode", mode);
        JsonArray measurements = JsonSupport.putArray(report, "measurements");
        try (var executor = Executors.newFixedThreadPool(workers)) {
            for (int repeat = 0; repeat < repeats; repeat++) {
                long started = System.nanoTime();
                long cpuStarted = processCpu();
                List<Callable<List<JsonObject>>> tasks = new ArrayList<>();
                for (int worker = 0; worker < workers; worker++) {
                    List<Path> chunk = new ArrayList<>();
                    for (int index = worker; index < paths.size(); index += workers) chunk.add(paths.get(index));
                    if (!chunk.isEmpty()) tasks.add(() -> configuration == null ? inspect(chunk, mode, null, null) :
                            new ReadOnlyTransaction(new MySqlConnectionFactory(), configuration).execute(connection -> inspect(chunk, mode, connection, configuration)));
                }
                List<JsonObject> results = new ArrayList<>();
                for (var future : executor.invokeAll(tasks)) results.addAll(future.get());
                results.sort(Comparator.comparing(item -> JsonSupport.stringOrEmpty(item.get("session"))));
                JsonArray sessions = JsonSupport.array();
                results.forEach(sessions::add);
                JsonObject measurement = JsonSupport.addObject(measurements);
                measurement.put("seconds", (System.nanoTime() - started) / 1e9);
                measurement.put("cpu_seconds", (processCpu() - cpuStarted) / 1e9);
                measurement.put("digest", CanonicalJson.sha256(sessions));
                measurement.put("sessions", sessions);
            }
        }
        return report;
    }

    private static JsonObject runDerived(JsonObject request, DatabaseConfiguration configuration) throws Exception {
        int workers = JsonSupport.integerOrDefault(request.get("workers"));
        int repeats = JsonSupport.integerOrDefault(request.get("repeats"), 4);
        List<String> paths = new ArrayList<>();
        JsonArray pathValues = JsonSupport.requireArray(request.get("paths"), "Study paths are required");
        for (Object item : pathValues) paths.add(JsonSupport.stringOrEmpty(item));
        Map<String, List<String>> neighbors = neighbors(request, paths);
        JsonObject report = JsonSupport.object();
        report.put("schema_version", 1); report.put("backend", "java"); report.put("workers", workers); report.put("mode", "derived");
        JsonArray measurements = JsonSupport.putArray(report, "measurements");
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
                List<JsonObject> results = new ArrayList<>();
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
                results.sort(Comparator.comparing(item -> JsonSupport.stringOrEmpty(item.get("session"))));
                JsonArray sessions = JsonSupport.array();
                results.forEach(sessions::add);
                JsonObject measurement = JsonSupport.addObject(measurements);
                measurement.put("seconds", (System.nanoTime() - started) / 1e9);
                measurement.put("cpu_seconds", (processCpu() - cpuStarted) / 1e9);
                measurement.put("query_count", queryCount);
                measurement.put("data_query_count", dataQueryCount);
                measurement.put("raw_rows", rawRows);
                measurement.put("derived_rows", derivedRows);
                measurement.put("digest", CanonicalJson.sha256(sessions));
                measurement.put("sessions", sessions);
            }
        }
        return report;
    }

    private static Map<String, List<String>> neighbors(JsonObject request, List<String> paths) {
        Object value = request.get("neighbors");
        if (!(value instanceof JsonObject neighbors)) throw new IllegalArgumentException("Derived neighbors are required");
        Map<String, List<String>> result = new HashMap<>();
        for (Map.Entry<String, Object> item : neighbors.entrySet()) {
            List<String> triple = new ArrayList<>();
            if (!(item.getValue() instanceof JsonArray values)) throw new IllegalArgumentException("Invalid derived neighbor triple");
            for (Object path : values) triple.add(JsonSupport.stringOrEmpty(path));
            if (triple.size() != 3 || !triple.get(1).equals(item.getKey()) || triple.stream().distinct().count() != 3) {
                throw new IllegalArgumentException("Invalid derived neighbor triple");
            }
            result.put(item.getKey(), triple);
        }
        if (!result.keySet().equals(new HashSet<>(paths))) throw new IllegalArgumentException("Incomplete derived neighbor map");
        return result;
    }

    private static DerivedChunk inspectDerived(List<String> targetPaths, Map<String, List<String>> neighbors, Connection connection, DatabaseConfiguration configuration) throws SQLException {
        List<JsonObject> sessions = new ArrayList<>();
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

    private static JsonObject derivedResult(LocalDate prior, LocalDate central, LocalDate following,
                                            Map<LocalDate, Map<String, ValidationContent.RawBar>> raw,
                                            Map<LocalDate, Map<String, ValidationContent.DerivedRow>> stored) {
        Map<String, ValidationContent.RawBar> previous = raw.getOrDefault(prior, Map.of());
        Map<String, ValidationContent.RawBar> current = raw.getOrDefault(central, Map.of());
        Map<String, ValidationContent.RawBar> next = raw.getOrDefault(following, Map.of());
        Map<String, ValidationContent.DerivedRow> actual = stored.getOrDefault(central, Map.of());
        JsonObject result = JsonSupport.object();
        result.put("session", central.toString());
        result.put("prior_session", prior.toString());
        result.put("following_session", following.toString());
        JsonObject counts = JsonSupport.putObject(result, "raw_counts");
        counts.put("prior", previous.size()); counts.put("current", current.size()); counts.put("following", next.size());
        result.put("derived_count", actual.size());
        result.put("expected_count", 0);
        JsonArray missing = JsonSupport.putArray(result, "missing_derived_keys");
        JsonArray extra = JsonSupport.putArray(result, "extra_derived_keys");
        JsonObject differences = JsonSupport.putObject(result, "column_difference_counts");
        LegacyDerivedCalculator.COLUMNS.forEach(column -> differences.put(column, 0));
        JsonObject ids = JsonSupport.putObject(result, "raw_ids");
        ids.put("prior", previous.isEmpty() ? null : ValidationContent.rawId(previous));
        ids.put("current", current.isEmpty() ? null : ValidationContent.rawId(current));
        ids.put("following", next.isEmpty() ? null : ValidationContent.rawId(next));
        result.put("expected_derived_id", null);
        result.put("stored_derived_id", null);
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
                differences.put(column, ((Number) differences.get(column)).intValue() + 1);
            }
        }
        result.put("expected_count", expected.size());
        result.put("expected_derived_id", LegacyDerivedCalculator.studyId(expected));
        result.put("stored_derived_id", LegacyDerivedCalculator.studyId(LegacyDerivedCalculator.values(actual)));
        result.put("state", "complete");
        result.put("reason", null);
        boolean matches = missingKeys.isEmpty() && extraKeys.isEmpty();
        for (String column : LegacyDerivedCalculator.COLUMNS) matches &= ((Number) differences.get(column)).intValue() == 0;
        result.put("matches", matches);
        return result;
    }

    private static ValidationContent.RawBar decodeStudyRaw(ResultSet rows, String ticker) throws SQLException {
        BigDecimal open = price(nullableDecimal(rows, 3));
        BigDecimal high = price(nullableDecimal(rows, 4));
        BigDecimal low = price(nullableDecimal(rows, 5));
        BigDecimal close = price(nullableDecimal(rows, 6));
        BigDecimal volume = nullableDecimal(rows, 7);
        BigDecimal vwap = optionalPrice(nullableDecimal(rows, 8));
        Long transactions = nullableLong(rows, 9);
        if (open == null || high == null || low == null || close == null || volume == null || volume.signum() < 0 || (transactions != null && transactions < 0) || (vwap != null && vwap.signum() <= 0) || low.compareTo(open.min(close)) > 0 || high.compareTo(open.max(close)) < 0 || low.compareTo(high) > 0) {
            throw new IllegalArgumentException("Invalid derived raw value");
        }
        return new ValidationContent.RawBar(ticker, open, high, low, close, volume, vwap, transactions);
    }

    private static BigDecimal price(BigDecimal value) {
        if (value == null || value.signum() <= 0 || value.compareTo(new BigDecimal("1e60")) >= 0) return null;
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

    private record DerivedChunk(List<JsonObject> sessions, int queryCount, int dataQueryCount, int rawRows, int derivedRows) {
    }

    private record DerivedScope(Map<LocalDate, Map<String, ValidationContent.RawBar>> raw,
                                Map<LocalDate, Map<String, ValidationContent.DerivedRow>> derived,
                                int queryCount, int rawRows, int derivedRows) {
    }

    private static long processCpu() {
        return ((com.sun.management.OperatingSystemMXBean) ManagementFactory.getOperatingSystemMXBean()).getProcessCpuTime();
    }

    private static List<JsonObject> inspect(List<Path> paths, String mode, Connection connection, DatabaseConfiguration configuration) {
        try {
            List<JsonObject> results = new ArrayList<>();
            for (Path path : paths) {
                LocalDate session = LocalDate.parse(path.getFileName().toString().replace(".ndjson", ""));
                JsonObject result = JsonSupport.object(); result.put("session", session.toString());
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
                if (mode.equals("reconcile")) result.put("matches", java.util.Objects.equals(result.get("archive_id"), result.get("database_id")));
                results.add(result);
            }
            return results;
        } catch (Exception error) {
            throw new IllegalStateException("Archive study inspection failed", error);
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
                    JsonObject row = JsonSupport.object(); row.put("T", rows.getString(1));
                    String[] prices = {"o", "h", "l", "c"};
                    for (int index = 0; index < prices.length; index++) row.put(prices[index], rows.getBigDecimal(index + 2));
                    row.put("v", rows.getBigDecimal(6));
                    BigDecimal vwap = rows.getBigDecimal(7);
                    row.put("vw", vwap);
                    long transactions = rows.getLong(8);
                    row.put("n", rows.wasNull() ? null : transactions);
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

    private static BigDecimal price(JsonObject row, String field) {
        Object value = row.get(field);
        if (!JsonSupport.isNumber(value) && !JsonSupport.isString(value)) throw new IllegalArgumentException("Invalid archive price");
        BigDecimal original = new BigDecimal(JsonSupport.stringOrEmpty(value));
        BigDecimal result = original.setScale(4, java.math.RoundingMode.HALF_UP);
        if (original.signum() <= 0 || original.compareTo(new BigDecimal("1e60")) >= 0 || result.signum() <= 0) throw new IllegalArgumentException("Invalid archive price");
        return result;
    }

    private static StudyBar decode(JsonObject row, LocalDate session) {
        Object value = row.get("v");
        if (!JsonSupport.isNumber(value)) throw new IllegalArgumentException("Invalid archive volume");
        BigDecimal volume = JsonSupport.decimalValue(value);
        if (volume.signum() < 0 || volume.compareTo(BigDecimal.valueOf(Long.MAX_VALUE)) > 0) throw new IllegalArgumentException("Invalid archive volume");
        Object ticker = row.get("T");
        if (!JsonSupport.isString(ticker) || JsonSupport.stringOrEmpty(ticker).isEmpty() || JsonSupport.stringOrEmpty(ticker).length() > 16 || JsonSupport.stringOrEmpty(ticker).codePoints().anyMatch(Character::isWhitespace)) throw new IllegalArgumentException("Invalid archive ticker");
        Object timestamp = row.get("t");
        if (!JsonSupport.isIntegralNumber(timestamp) || !JsonSupport.fitsLong(timestamp) || JsonSupport.longIntegerValue(timestamp) < 0 || !java.time.Instant.ofEpochMilli(JsonSupport.longIntegerValue(timestamp)).atZone(java.time.ZoneId.of("America/New_York")).toLocalDate().equals(session)) throw new IllegalArgumentException("Invalid archive timestamp");
        Object transactions = row.get("n");
        if (transactions != null && (!JsonSupport.isIntegralNumber(transactions) || !JsonSupport.fitsLong(transactions) || JsonSupport.longIntegerValue(transactions) < 0)) throw new IllegalArgumentException("Invalid transactions");
        Object vwap = row.get("vw");
        StudyBar bar = new StudyBar(JsonSupport.stringOrEmpty(ticker), price(row, "o"), price(row, "h"), price(row, "l"), price(row, "c"), volume, vwap == null ? null : price(row, "vw"), transactions == null ? null : JsonSupport.longIntegerValue(transactions));
        if (bar.low().compareTo(bar.open().min(bar.close())) > 0 || bar.high().compareTo(bar.open().max(bar.close())) < 0 || bar.low().compareTo(bar.high()) > 0) throw new IllegalArgumentException("Inconsistent archive OHLC");
        return bar;
    }

    private static JsonArray payload(List<StudyBar> bars) {
        JsonArray result = JsonSupport.array();
        for (var item : bars) {
            var bar = item;
            JsonObject row = JsonSupport.addObject(result);
            row.put("T", bar.ticker()); row.put("o", bar.open().toPlainString()); row.put("h", bar.high().toPlainString()); row.put("l", bar.low().toPlainString()); row.put("c", bar.close().toPlainString()); row.put("v", item.volume().stripTrailingZeros().toPlainString());
            row.put("vw", bar.vwap() == null ? null : bar.vwap().toPlainString());
            row.put("n", bar.transactions());
        }
        return result;
    }
}
