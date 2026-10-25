package stockguidance.migration;

import stockguidance.database.SqlIdentifiers;
import stockguidance.database.TableNames;
import stockguidance.validation.ValidationContent;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.BasicFileAttributes;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.TimeUnit;

final class OperationalDatabase {
    enum InputAction { MISSING, TRUSTED, BASELINE, IMPORT }

    record Receipt(String rawId, String availableAt, String dependencyId, String derivedId) { }

    record SourceFile(Fingerprint fingerprint, boolean pending) { }

    record Fingerprint(long size, long modifiedNs) {
        static Fingerprint read(Path path) throws IOException {
            BasicFileAttributes attributes = Files.readAttributes(path, BasicFileAttributes.class);
            if (!attributes.isRegularFile()) throw new IOException("Source is not a regular file: " + path);
            return new Fingerprint(attributes.size(), attributes.lastModifiedTime().to(TimeUnit.NANOSECONDS));
        }
    }

    private OperationalDatabase() { }

    static InputAction planInput(Receipt receipt, Fingerprint stored, Fingerprint current, boolean refreshed) {
        if (current == null) return receipt != null && !refreshed ? InputAction.TRUSTED : InputAction.MISSING;
        if (receipt == null || refreshed) return InputAction.IMPORT;
        if (stored == null) return InputAction.BASELINE;
        return stored.equals(current) ? InputAction.TRUSTED : InputAction.IMPORT;
    }

    static Map<LocalDate, Receipt> receipts(Connection connection, TableNames names, List<LocalDate> dates) throws SQLException {
        Map<LocalDate, Receipt> result = new HashMap<>();
        if (dates.isEmpty()) return result;
        String parameters = String.join(",", Collections.nCopies(dates.size(), "?"));
        try (PreparedStatement statement = connection.prepareStatement("SELECT i.session_date, i.raw_id, i.available_at, i.derived_dependency_id, i.derived_content_id FROM "
                + SqlIdentifiers.quote(names.imports(), "imports") + " i JOIN " + SqlIdentifiers.quote(names.sessions(), "sessions")
                + " s ON s.transaction_date = i.session_date WHERE i.session_date IN (" + parameters + ")")) {
            bindDates(statement, dates);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    LocalDate session = rows.getDate(1).toLocalDate();
                    Receipt receipt = new Receipt(rows.getString(2), rows.getString(3), rows.getString(4), rows.getString(5));
                    if (receipt.rawId() == null || receipt.availableAt() == null || receipt.availableAt().isBlank()
                            || result.putIfAbsent(session, receipt) != null) throw new IllegalArgumentException("Invalid database receipt: " + session);
                }
            }
        }
        return result;
    }

    static Map<LocalDate, SourceFile> fingerprints(Connection connection, String sourceKey) throws SQLException {
        Map<LocalDate, SourceFile> result = new HashMap<>();
        try (PreparedStatement statement = connection.prepareStatement(
                "SELECT session_date, file_size, modified_ns, download_pending FROM stockguidance_daily_source_files WHERE source_key = ?")) {
            statement.setString(1, sourceKey);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) result.put(rows.getDate(1).toLocalDate(), new SourceFile(new Fingerprint(rows.getLong(2), rows.getLong(3)), rows.getBoolean(4)));
            }
        }
        return result;
    }

    static void saveFingerprint(Connection connection, String sourceKey, LocalDate session, Fingerprint fingerprint) throws SQLException {
        try (PreparedStatement statement = connection.prepareStatement("INSERT INTO stockguidance_daily_source_files "
                + "(source_key, session_date, file_size, modified_ns) VALUES (?, ?, ?, ?) "
                + "ON DUPLICATE KEY UPDATE file_size = VALUES(file_size), modified_ns = VALUES(modified_ns), download_pending = 0")) {
            statement.setString(1, sourceKey);
            statement.setObject(2, session);
            statement.setLong(3, fingerprint.size());
            statement.setLong(4, fingerprint.modifiedNs());
            statement.executeUpdate();
        }
    }

    static void requireNoPartialImports(Connection connection, List<LocalDate> dates) throws SQLException {
        try (PreparedStatement statement = connection.prepareStatement("SELECT session_date FROM stockguidance_daily_import_in_progress ORDER BY session_date");
             ResultSet rows = statement.executeQuery()) {
            while (rows.next()) {
                LocalDate session = rows.getDate(1).toLocalDate();
                if (dates.contains(session)) throw new IllegalArgumentException("Unfinished raw import for " + session + "; run --stage source first");
            }
        }
    }

    static String dependency(LocalDate session, JavaWriterManifest manifest, Map<LocalDate, Receipt> receipts) {
        List<LocalDate> neighbors = manifest.target(session).derivedDependencies();
        return ValidationContent.dependencyId(identity(neighbors.get(0), receipts), identity(session, receipts), identity(neighbors.get(1), receipts));
    }

    private static String identity(LocalDate session, Map<LocalDate, Receipt> receipts) {
        Receipt receipt = receipts.get(session);
        return receipt == null ? ArchiveCandidateReader.recordsId(Map.of()) : receipt.rawId();
    }

    static Map<LocalDate, ArchiveCandidateReader.SessionArchive> readRaw(Connection connection, TableNames names,
            List<LocalDate> dates, Map<LocalDate, Receipt> receipts) throws SQLException {
        Map<LocalDate, Map<String, ValidationContent.RawBar>> bars = new HashMap<>();
        for (LocalDate session : dates) bars.put(session, new TreeMap<>());
        List<LocalDate> present = dates.stream().filter(receipts::containsKey).toList();
        if (!present.isEmpty()) {
            String parameters = String.join(",", Collections.nCopies(present.size(), "?"));
            String sql = "SELECT s.transaction_date, t.label, p.open, p.high, p.low, p.close, p.volume, p.vwap, p.transactions FROM "
                    + SqlIdentifiers.quote(names.prices(), "prices") + " p JOIN " + SqlIdentifiers.quote(names.sessions(), "sessions")
                    + " s ON s.id = p.date_id JOIN " + SqlIdentifiers.quote(names.tickers(), "tickers")
                    + " t ON t.id = p.ticker_id WHERE s.transaction_date IN (" + parameters + ") ORDER BY s.transaction_date, t.label";
            try (PreparedStatement statement = connection.prepareStatement(sql)) {
                bindDates(statement, present);
                statement.setFetchSize(1000);
                try (ResultSet rows = statement.executeQuery()) {
                    while (rows.next()) {
                        String ticker = rows.getString(2);
                        long count = rows.getLong(9);
                        Long transactions = rows.wasNull() ? null : count;
                        ValidationContent.RawBar bar = new ValidationContent.RawBar(ticker, rows.getBigDecimal(3), rows.getBigDecimal(4),
                                rows.getBigDecimal(5), rows.getBigDecimal(6), rows.getBigDecimal(7), rows.getBigDecimal(8), transactions);
                        if (bars.get(rows.getDate(1).toLocalDate()).putIfAbsent(ticker, bar) != null) {
                            throw new IllegalArgumentException("Duplicate database raw ticker");
                        }
                    }
                }
            }
        }
        Map<LocalDate, ArchiveCandidateReader.SessionArchive> result = new HashMap<>();
        for (LocalDate session : dates) {
            if (receipts.containsKey(session) && bars.get(session).isEmpty()) throw new IllegalArgumentException("Completed raw session has no rows: " + session);
            result.put(session, new ArchiveCandidateReader.SessionArchive(null, session, null, Map.copyOf(bars.get(session)), identity(session, receipts)));
        }
        return result;
    }

    static List<List<LocalDate>> batches(List<LocalDate> dates, int size) {
        List<List<LocalDate>> result = new ArrayList<>();
        for (int offset = 0; offset < dates.size(); offset += size) result.add(dates.subList(offset, Math.min(offset + size, dates.size())));
        return result;
    }

    private static void bindDates(PreparedStatement statement, List<LocalDate> dates) throws SQLException {
        for (int index = 0; index < dates.size(); index++) statement.setObject(index + 1, dates.get(index));
    }
}
