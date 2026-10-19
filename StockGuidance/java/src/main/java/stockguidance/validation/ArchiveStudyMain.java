package stockguidance.validation;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.MySqlConnectionFactory;
import stockguidance.database.ReadOnlyTransaction;
import stockguidance.database.SqlIdentifiers;
import stockguidance.json.CanonicalJson;
import stockguidance.json.JsonSupport;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.math.BigDecimal;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.Date;
import java.sql.ResultSet;
import java.time.LocalDate;
import java.lang.management.ManagementFactory;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.List;
import java.util.TreeMap;
import java.util.concurrent.Callable;
import java.util.concurrent.Executors;

public final class ArchiveStudyMain {
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
        if (workers < 1 || workers > 3 || repeats < 1 || repeats > 10 || !List.of("files", "database", "reconcile").contains(mode)) {
            throw new IllegalArgumentException("Invalid study settings");
        }
        List<Path> paths = new ArrayList<>();
        for (JsonNode item : request.path("paths")) paths.add(Path.of(item.asText()));
        if (paths.isEmpty() || paths.stream().distinct().count() != paths.size()) throw new IllegalArgumentException("Distinct paths required");
        DatabaseConfiguration configuration = mode.equals("files") ? null : DatabaseConfiguration.load(Path.of(request.path("dbinfo_file").asText()));
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
