package stockguidance.validation;

import com.fasterxml.jackson.databind.JsonNode;
import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.ReadOnlyTransaction;
import stockguidance.json.JsonSupport;

import java.io.BufferedReader;
import java.io.IOException;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.Date;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.regex.Pattern;

/** Read-only comparison of legacy grouped-daily files with raw database rows. */
public final class LegacyDownloadComparator {
    private static final Pattern FILE_NAME = Pattern.compile("\\d{4}-\\d{2}-\\d{2}\\.ndjson");
    private static final ZoneId EXCHANGE_ZONE = ZoneId.of("America/New_York");
    private static final BigDecimal PRICE_SCALE = new BigDecimal("0.0001");
    private static final BigDecimal PRICE_LIMIT = new BigDecimal("1000000000000000");
    private static final int MAX_FINDINGS = 10_000;

    private final stockguidance.database.ConnectionFactory connectionFactory;

    public LegacyDownloadComparator(stockguidance.database.ConnectionFactory connectionFactory) {
        this.connectionFactory = connectionFactory;
    }

    public ObjectNodeResult compare(DatabaseConfiguration configuration, Path dataRoot, LocalDate requestedStart, LocalDate requestedEnd) {
        return compare(configuration, dataRoot, requestedStart, requestedEnd, "adjusted");
    }

    public ObjectNodeResult compare(DatabaseConfiguration configuration, Path dataRoot, LocalDate requestedStart, LocalDate requestedEnd, String adjustment) {
        if (configuration == null || dataRoot == null) {
            throw new IllegalArgumentException("Archive comparison requires explicit configuration and data root");
        }
        if (!"adjusted".equals(adjustment) && !"unadjusted".equals(adjustment)) {
            throw new IllegalArgumentException("Archive adjustment must be adjusted or unadjusted");
        }
        Inventory inventory = inventory(dataRoot, requestedStart, requestedEnd);
        return new ReadOnlyTransaction(connectionFactory, configuration).execute(connection -> inspect(connection, inventory, requestedStart, requestedEnd, adjustment));
    }

    private Inventory inventory(Path dataRoot, LocalDate requestedStart, LocalDate requestedEnd) {
        Path root = dataRoot.resolve("market_summary");
        if (!Files.isDirectory(root)) {
            throw new IllegalArgumentException("Legacy market_summary directory is missing");
        }
        List<FileEntry> files = new ArrayList<>();
        try (var years = Files.list(root)) {
            years.filter(Files::isDirectory).sorted().forEach(year -> {
                try (var entries = Files.list(year)) {
                    entries.filter(Files::isRegularFile).sorted().forEach(path -> {
                        String name = path.getFileName().toString();
                        if (!FILE_NAME.matcher(name).matches()) {
                            return;
                        }
                        LocalDate date;
                        try {
                            date = LocalDate.parse(name.substring(0, 10));
                        } catch (DateTimeParseException error) {
                            throw new ArchiveComparisonException("Invalid legacy download filename");
                        }
                        if (date.getYear() != Integer.parseInt(year.getFileName().toString()) || outside(date, requestedStart, requestedEnd)) {
                            return;
                        }
                        files.add(new FileEntry(date, path));
                    });
                } catch (IOException error) {
                    throw new ArchiveComparisonException("Legacy download directory could not be read");
                }
            });
        } catch (IOException error) {
            throw new ArchiveComparisonException("Legacy download directory could not be read");
        }
        files.sort(Comparator.comparing(FileEntry::date).thenComparing(item -> item.path().toString()));
        return new Inventory(files);
    }

    private ObjectNodeResult inspect(Connection connection, Inventory inventory, LocalDate requestedStart, LocalDate requestedEnd, String adjustment) throws SQLException {
        FindingCollector findings = new FindingCollector();
        if (inventory.files().isEmpty()) {
            findings.add("invalid", "archive", "No legacy NDJSON download files were found in the requested scope", null, null);
        }
        Map<LocalDate, List<FileEntry>> filesByDate = new TreeMap<>();
        for (FileEntry file : inventory.files()) {
            filesByDate.computeIfAbsent(file.date(), ignored -> new ArrayList<>()).add(file);
        }
        Set<LocalDate> databaseDates = readDatabaseDates(connection, requestedStart, requestedEnd);
        for (LocalDate date : filesByDate.keySet()) {
            if (!databaseDates.contains(date)) {
                findings.add("missing", "date", "Downloaded file has no transaction_dates mapping", date, null);
            }
        }
        for (LocalDate date : databaseDates) {
            if (!filesByDate.containsKey(date)) {
                findings.add("missing", "date", "Database date has no downloaded legacy file", date, null);
            }
        }

        String sql = "SELECT t.transaction_date, s.label, r.open, r.high, r.low, r.close, r.volume, r.vwap, r.transactions " +
                "FROM raw_stock_data r JOIN ticker_symbol s ON s.id = r.ticker_id JOIN transaction_dates t ON t.id = r.date_id " +
                rangePredicate("t.transaction_date", requestedStart, requestedEnd) +
                "ORDER BY t.transaction_date, s.label";
        try (PreparedStatement statement = connection.prepareStatement(sql, ResultSet.TYPE_FORWARD_ONLY, ResultSet.CONCUR_READ_ONLY)) {
            bindRange(statement, requestedStart, requestedEnd);
            statement.setFetchSize(1000);
            try (ResultSet rows = statement.executeQuery()) {
                DbCursor cursor = new DbCursor(rows);
                for (Map.Entry<LocalDate, List<FileEntry>> entry : filesByDate.entrySet()) {
                    LocalDate date = entry.getKey();
                    while (cursor.current != null && cursor.current.date().isBefore(date)) {
                        findings.add("extra", "ticker", "Database raw row has no downloaded record", cursor.current.date(), cursor.current.ticker());
                        cursor.advance();
                    }
                    if (cursor.current != null && cursor.current.date().equals(date)) {
                        FileSnapshot snapshot = readFiles(entry.getValue(), findings);
                        compareDate(snapshot, cursor, findings);
                    } else {
                        readFiles(entry.getValue(), findings);
                    }
                }
                while (cursor.current != null) {
                    findings.add("extra", "ticker", "Database raw row has no downloaded record", cursor.current.date(), cursor.current.ticker());
                    cursor.advance();
                }
            }
        }
        ObjectNodeResult result = new ObjectNodeResult(requestedStart, requestedEnd, adjustment, inventory.files().size(), findings);
        return result;
    }

    private Set<LocalDate> readDatabaseDates(Connection connection, LocalDate requestedStart, LocalDate requestedEnd) throws SQLException {
        String sql = "SELECT transaction_date FROM transaction_dates" + rangePredicate("transaction_date", requestedStart, requestedEnd) + "ORDER BY transaction_date";
        Set<LocalDate> result = new HashSet<>();
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            bindRange(statement, requestedStart, requestedEnd);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    result.add(rows.getDate(1).toLocalDate());
                }
            }
        }
        return result;
    }

    private static String rangePredicate(String column, LocalDate start, LocalDate end) {
        StringBuilder result = new StringBuilder(" WHERE 1=1");
        if (start != null) result.append(" AND ").append(column).append(" >= ?");
        if (end != null) result.append(" AND ").append(column).append(" <= ?");
        return result.append(' ').toString();
    }

    private static void bindRange(PreparedStatement statement, LocalDate start, LocalDate end) throws SQLException {
        int index = 1;
        if (start != null) statement.setDate(index++, Date.valueOf(start));
        if (end != null) statement.setDate(index, Date.valueOf(end));
    }

    private FileSnapshot readFiles(List<FileEntry> entries, FindingCollector findings) {
        if (entries.size() > 1) {
            for (FileEntry entry : entries) {
                findings.add("invalid", "duplicate_file", "More than one downloaded file claims this session date", entry.date(), null);
            }
        }
        Map<String, Bar> bars = new TreeMap<>();
        for (FileEntry entry : entries) {
            try (BufferedReader reader = Files.newBufferedReader(entry.path(), StandardCharsets.UTF_8)) {
                String line;
                int lineNumber = 0;
                while ((line = reader.readLine()) != null) {
                    lineNumber++;
                    if (line.isBlank()) {
                        findings.add("invalid", "file", "Blank NDJSON line", entry.date(), null);
                        continue;
                    }
                    try {
                        Bar bar = decodeBar(JsonSupport.parse(line), entry.date());
                        if (bars.putIfAbsent(bar.ticker(), bar) != null) {
                            findings.add("invalid", "duplicate", "Downloaded file contains a duplicate ticker", entry.date(), bar.ticker());
                        }
                    } catch (RuntimeException error) {
                        findings.add("invalid", "file", "Downloaded record is malformed at line " + lineNumber, entry.date(), null);
                    }
                }
            } catch (IOException error) {
                findings.add("invalid", "file", "Downloaded file could not be read", entry.date(), null);
            }
        }
        return new FileSnapshot(entries.get(0).date(), bars);
    }

    private void compareDate(FileSnapshot snapshot, DbCursor cursor, FindingCollector findings) throws SQLException {
        Map<String, Bar> databaseBars = new HashMap<>();
        while (cursor.current != null && cursor.current.date().equals(snapshot.date())) {
            DbBar row = cursor.current;
            if (databaseBars.putIfAbsent(row.ticker(), row.bar()) != null) {
                findings.add("invalid", "duplicate", "Database contains a duplicate raw ticker", snapshot.date(), row.ticker());
            }
            cursor.advance();
        }
        for (String ticker : snapshot.bars().keySet()) {
            if (!databaseBars.containsKey(ticker)) {
                findings.add("missing", "ticker", "Downloaded record is absent from raw_stock_data", snapshot.date(), ticker);
            }
        }
        for (Map.Entry<String, Bar> entry : databaseBars.entrySet()) {
            Bar downloaded = snapshot.bars().get(entry.getKey());
            if (downloaded == null) {
                findings.add("extra", "ticker", "Raw database record is absent from downloaded file", snapshot.date(), entry.getKey());
            } else if (!downloaded.equals(entry.getValue())) {
                findings.add("invalid", "value", "Raw database values differ from downloaded file", snapshot.date(), entry.getKey());
            }
        }
    }

    private static Bar decodeBar(JsonNode value, LocalDate expectedDate) {
        if (value == null || !value.isObject()) {
            throw new IllegalArgumentException("Downloaded record must be an object");
        }
        String ticker = text(value, "T");
        if (ticker.length() > 16 || ticker.chars().anyMatch(Character::isWhitespace)) {
            throw new IllegalArgumentException("Downloaded ticker key is invalid");
        }
        long timestamp = integer(value, "t");
        LocalDate actualDate = Instant.ofEpochMilli(timestamp).atZone(EXCHANGE_ZONE).toLocalDate();
        if (!expectedDate.equals(actualDate)) {
            throw new IllegalArgumentException("Downloaded timestamp belongs to another session");
        }
        Bar bar = new Bar(ticker, price(value, "o"), price(value, "h"), price(value, "l"), price(value, "c"),
                integer(value, "v"), nullablePrice(value, "vw"), nullableInteger(value, "n"));
        if (bar.low().compareTo(bar.open()) > 0 || bar.low().compareTo(bar.close()) > 0 ||
                bar.high().compareTo(bar.open()) < 0 || bar.high().compareTo(bar.close()) < 0 || bar.low().compareTo(bar.high()) > 0) {
            throw new IllegalArgumentException("Downloaded OHLC values are inconsistent");
        }
        return bar;
    }

    private static String text(JsonNode value, String name) {
        JsonNode field = value.get(name);
        if (field == null || !field.isTextual() || field.textValue().isBlank()) {
            throw new IllegalArgumentException("Downloaded text field is invalid");
        }
        return field.textValue();
    }

    private static long integer(JsonNode value, String name) {
        JsonNode field = value.get(name);
        if (field == null || !field.isIntegralNumber() || field.isBoolean() || field.longValue() < 0) {
            throw new IllegalArgumentException("Downloaded integer field is invalid");
        }
        return field.longValue();
    }

    private static BigDecimal price(JsonNode value, String name) {
        BigDecimal result = nullablePrice(value, name);
        if (result == null) {
            throw new IllegalArgumentException("Downloaded price field is missing");
        }
        return result;
    }

    private static BigDecimal nullablePrice(JsonNode value, String name) {
        JsonNode field = value.get(name);
        if (field == null || field.isNull()) {
            return null;
        }
        if (!field.isNumber() && !field.isTextual()) {
            throw new IllegalArgumentException("Downloaded price field is invalid");
        }
        BigDecimal result = new BigDecimal(field.asText()).setScale(4, RoundingMode.HALF_UP);
        if (result.signum() <= 0 || result.compareTo(PRICE_LIMIT) >= 0) {
            throw new IllegalArgumentException("Downloaded price is outside the supported range");
        }
        return result;
    }

    private static Long nullableInteger(JsonNode value, String name) {
        JsonNode field = value.get(name);
        if (field == null || field.isNull()) {
            return null;
        }
        return integer(value, name);
    }

    private static boolean outside(LocalDate date, LocalDate start, LocalDate end) {
        return (start != null && date.isBefore(start)) || (end != null && date.isAfter(end));
    }

    private record FileEntry(LocalDate date, Path path) { }
    private record Inventory(List<FileEntry> files) { }
    private record FileSnapshot(LocalDate date, Map<String, Bar> bars) { }
    private record Bar(String ticker, BigDecimal open, BigDecimal high, BigDecimal low, BigDecimal close, long volume, BigDecimal vwap, Long transactions) { }
    private record DbBar(LocalDate date, String ticker, Bar bar) { }

    private static final class DbCursor {
        private final ResultSet rows;
        private DbBar current;

        private DbCursor(ResultSet rows) throws SQLException {
            this.rows = rows;
            advance();
        }

        private void advance() throws SQLException {
            if (!rows.next()) {
                current = null;
                return;
            }
            LocalDate date = rows.getDate(1).toLocalDate();
            String ticker = rows.getString(2);
            current = new DbBar(date, ticker, new Bar(ticker, sqlPrice(rows, 3), sqlPrice(rows, 4), sqlPrice(rows, 5), sqlPrice(rows, 6),
                    rows.getLong(7), sqlNullablePrice(rows, 8), sqlNullableLong(rows, 9)));
        }

        private static BigDecimal sqlPrice(ResultSet rows, int index) throws SQLException {
            BigDecimal value = rows.getBigDecimal(index);
            if (value == null) {
                throw new SQLException("Database raw price is null");
            }
            return value.setScale(4, RoundingMode.HALF_UP);
        }

        private static BigDecimal sqlNullablePrice(ResultSet rows, int index) throws SQLException {
            BigDecimal value = rows.getBigDecimal(index);
            return value == null ? null : value.setScale(4, RoundingMode.HALF_UP);
        }

        private static Long sqlNullableLong(ResultSet rows, int index) throws SQLException {
            long value = rows.getLong(index);
            return rows.wasNull() ? null : value;
        }
    }

    private static final class FindingCollector {
        private final List<Finding> findings = new ArrayList<>();
        private int omitted;

        private void add(String category, String check, String detail, LocalDate date, String ticker) {
            if (findings.size() < MAX_FINDINGS) {
                findings.add(new Finding(category, check, detail, date, ticker));
            } else {
                omitted++;
            }
        }
    }

    private record Finding(String category, String check, String detail, LocalDate date, String ticker) { }

    public static final class ObjectNodeResult {
        private final LocalDate start;
        private final LocalDate end;
        private final String adjustment;
        private final int fileCount;
        private final FindingCollector collector;

        private ObjectNodeResult(LocalDate start, LocalDate end, String adjustment, int fileCount, FindingCollector collector) {
            this.start = start;
            this.end = end;
            this.adjustment = adjustment;
            this.fileCount = fileCount;
            this.collector = collector;
        }

        public com.fasterxml.jackson.databind.node.ObjectNode report() {
            var report = com.fasterxml.jackson.databind.node.JsonNodeFactory.instance.objectNode();
            report.put("schema_version", 1);
            report.put("backend", "java");
            report.put("operation", "compare_database_downloads");
            report.put("adjustment", adjustment);
            report.put("complete", collector.findings.isEmpty());
            report.put("failed", false);
            report.put("exit_code", collector.findings.isEmpty() ? 0 : 2);
            var scope = report.putObject("scope");
            if (start == null) scope.putNull("start"); else scope.put("start", start.toString());
            if (end == null) scope.putNull("end"); else scope.put("end", end.toString());
            report.put("download_file_count", fileCount);
            report.put("finding_count", collector.findings.size());
            report.put("omitted_finding_count", collector.omitted);
            var findings = report.putArray("findings");
            collector.findings.stream().sorted(Comparator.comparing((Finding item) -> item.date() == null ? "" : item.date().toString()).thenComparing(Finding::check).thenComparing(item -> item.ticker() == null ? "" : item.ticker())).forEach(item -> {
                var finding = findings.addObject();
                finding.put("category", item.category());
                finding.put("check", item.check());
                finding.put("detail", item.detail());
                if (item.date() == null) finding.putNull("session_date"); else finding.put("session_date", item.date().toString());
                if (item.ticker() == null) finding.putNull("ticker"); else finding.put("ticker", item.ticker());
            });
            return report;
        }
    }

    private static final class ArchiveComparisonException extends RuntimeException {
        private ArchiveComparisonException(String message) {
            super(message);
        }
    }
}
