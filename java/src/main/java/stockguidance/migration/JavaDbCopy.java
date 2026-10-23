package stockguidance.migration;

import stockguidance.database.TableNames;
import stockguidance.database.SqlIdentifiers;

import java.sql.Connection;
import java.sql.Date;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

public final class JavaDbCopy implements AutoCloseable {
    public record RawRow(long tickerId, long dateId, Double close) { }
    public record DeltaRow(long tickerId, long dateId, Double closeRatioToPrior) { }
    public record SessionData(Long dateId, JavaZScoreCandidate.Evidence evidence,
                              Map<Long, RawRow> raw, Map<Long, DeltaRow> deltas) {
        public SessionData {
            raw = Collections.unmodifiableMap(new HashMap<>(raw));
            deltas = Collections.unmodifiableMap(new HashMap<>(deltas));
        }
    }

    @FunctionalInterface
    interface BatchLoader extends AutoCloseable {
        Map<LocalDate, SessionData> load(List<LocalDate> dates) throws Exception;
        default void close() throws Exception { }
    }

    private final Map<LocalDate, CompletableFuture<SessionData>> sessions = new HashMap<>();
    private final ExecutorService queries = Executors.newSingleThreadExecutor();
    private final BatchLoader loader;
    private final Map<Long, String> tickers;
    private boolean closed;
    private long batches;

    JavaDbCopy(Map<Long, String> tickers, BatchLoader loader) {
        this.tickers = Map.copyOf(tickers);
        this.loader = loader;
    }

    static JavaDbCopy open(Connection connection, JavaWriterManifest manifest) throws SQLException {
        try {
            connection.setTransactionIsolation(Connection.TRANSACTION_REPEATABLE_READ);
            connection.setReadOnly(true);
            connection.setAutoCommit(false);
            TableNames names = manifest.tableNames();
            Map<Long, String> current = new HashMap<>();
            try (PreparedStatement statement = connection.prepareStatement(
                    "SELECT id, label FROM " + SqlIdentifiers.quote(names.tickers(), "tickers") + " ORDER BY id");
                 ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    if (current.putIfAbsent(rows.getLong(1), rows.getString(2)) != null) {
                        throw new IllegalArgumentException("Duplicate ticker mapping");
                    }
                }
            }
            if (!current.equals(manifest.tickerMappings())) throw new IllegalArgumentException("Ticker registry changed");
            return new JavaDbCopy(current, new JdbcLoader(connection, manifest));
        } catch (SQLException | RuntimeException error) {
            try { connection.rollback(); } finally { connection.close(); }
            throw error;
        }
    }

    public synchronized CompletableFuture<Map<LocalDate, SessionData>> request(List<LocalDate> dates) {
        if (closed) throw new IllegalStateException("JavaDbCopy is closed");
        List<LocalDate> missing = new ArrayList<>();
        for (LocalDate date : dates) {
            if (!sessions.containsKey(date)) {
                sessions.put(date, new CompletableFuture<>());
                missing.add(date);
            }
        }
        for (int offset = 0; offset < missing.size(); offset += 20) {
            List<LocalDate> batch = List.copyOf(missing.subList(offset, Math.min(offset + 20, missing.size())));
            Map<LocalDate, CompletableFuture<SessionData>> pending = new LinkedHashMap<>();
            for (LocalDate date : batch) pending.put(date, sessions.get(date));
            batches++;
            queries.execute(() -> {
                try {
                    Map<LocalDate, SessionData> loaded = loader.load(batch);
                    if (!loaded.keySet().equals(pending.keySet())) throw new IllegalStateException("Incomplete source batch");
                    for (LocalDate date : batch) {
                        if (loaded.get(date) == null) throw new IllegalStateException("Missing source session");
                    }
                    pending.forEach((date, future) -> future.complete(loaded.get(date)));
                } catch (Exception error) {
                    pending.values().forEach(future -> future.completeExceptionally(error));
                }
            });
        }
        Map<LocalDate, CompletableFuture<SessionData>> requested = new LinkedHashMap<>();
        for (LocalDate date : dates) requested.put(date, sessions.get(date));
        return CompletableFuture.allOf(requested.values().toArray(CompletableFuture[]::new)).thenApply(ignored -> {
            Map<LocalDate, SessionData> result = new LinkedHashMap<>();
            requested.forEach((date, future) -> result.put(date, future.join()));
            return Map.copyOf(result);
        });
    }

    Map<LocalDate, SessionData> get(List<LocalDate> dates) {
        try {
            return request(dates).join();
        } catch (CompletionException error) {
            throw new IllegalStateException("JavaDbCopy source batch failed", error.getCause());
        }
    }

    Map<Long, String> tickers() { return tickers; }
    synchronized long batchCount() { return batches; }
    synchronized int sessionCount() { return sessions.size(); }

    @Override
    public void close() throws Exception {
        synchronized (this) { closed = true; }
        queries.shutdown();
        if (!queries.awaitTermination(30, TimeUnit.SECONDS)) {
            queries.shutdownNow();
            loader.close();
            throw new IllegalStateException("Source loader did not stop");
        }
        loader.close();
    }

    private static final class JdbcLoader implements BatchLoader {
        private final Connection connection;
        private final JavaWriterManifest manifest;

        private JdbcLoader(Connection connection, JavaWriterManifest manifest) {
            this.connection = connection;
            this.manifest = manifest;
        }

        @Override
        public Map<LocalDate, SessionData> load(List<LocalDate> dates) throws SQLException {
            TableNames names = manifest.tableNames();
            Map<LocalDate, Long> identifiers = new HashMap<>();
            Map<LocalDate, JavaZScoreCandidate.Evidence> evidence = new HashMap<>();
            for (LocalDate date : dates) {
                if (manifest.isAdoptable(date)) manifest.archive(date);
                var file = manifest.file(date);
                evidence.put(date, new JavaZScoreCandidate.Evidence(file.valid(), file.recordsId(), null, null));
            }
            String parameters = String.join(",", Collections.nCopies(dates.size(), "?"));
            try (PreparedStatement statement = connection.prepareStatement("SELECT id, transaction_date FROM "
                    + SqlIdentifiers.quote(names.sessions(), "sessions") + " WHERE transaction_date IN (" + parameters + ")")) {
                for (int index = 0; index < dates.size(); index++) statement.setDate(index + 1, Date.valueOf(dates.get(index)));
                try (ResultSet rows = statement.executeQuery()) {
                    while (rows.next()) {
                        if (identifiers.putIfAbsent(rows.getDate(2).toLocalDate(), rows.getLong(1)) != null) {
                            throw new IllegalArgumentException("Duplicate date mapping");
                        }
                    }
                }
            }
            try (PreparedStatement statement = connection.prepareStatement("SELECT session_date, raw_id, available_at FROM "
                    + SqlIdentifiers.quote(names.imports(), "imports") + " WHERE session_date IN (" + parameters + ")")) {
                for (int index = 0; index < dates.size(); index++) statement.setDate(index + 1, Date.valueOf(dates.get(index)));
                try (ResultSet rows = statement.executeQuery()) {
                    while (rows.next()) {
                        LocalDate date = rows.getDate(1).toLocalDate();
                        var previous = evidence.get(date);
                        if (previous == null || previous.rawId() != null) throw new IllegalArgumentException("Invalid source evidence");
                        evidence.put(date, new JavaZScoreCandidate.Evidence(previous.expected(), previous.expectedId(),
                                rows.getString(2), rows.getString(3)));
                    }
                }
            }
            Map<Long, LocalDate> byId = new HashMap<>();
            identifiers.forEach((date, identifier) -> {
                if (byId.putIfAbsent(identifier, date) != null) throw new IllegalArgumentException("Conflicting date mapping");
            });
            Map<LocalDate, Map<Long, RawRow>> raw = new HashMap<>();
            Map<LocalDate, Map<Long, DeltaRow>> deltas = new HashMap<>();
            for (LocalDate date : dates) {
                raw.put(date, new HashMap<>());
                deltas.put(date, new HashMap<>());
            }
            if (!byId.isEmpty()) {
                readValues(names.prices(), "close", byId, (date, tickerId, dateId, value) -> {
                    if (raw.get(date).putIfAbsent(tickerId, new RawRow(tickerId, dateId, value)) != null) {
                        throw new IllegalArgumentException("Duplicate raw input");
                    }
                });
                readValues(names.deltas(), "close_ratio_to_prior", byId, (date, tickerId, dateId, value) -> {
                    if (deltas.get(date).putIfAbsent(tickerId, new DeltaRow(tickerId, dateId, value)) != null) {
                        throw new IllegalArgumentException("Duplicate delta input");
                    }
                });
            }
            Map<LocalDate, SessionData> result = new HashMap<>();
            for (LocalDate date : dates) result.put(date, new SessionData(identifiers.get(date), evidence.get(date), raw.get(date), deltas.get(date)));
            return result;
        }

        private void readValues(String table, String column, Map<Long, LocalDate> dates, RowConsumer consumer) throws SQLException {
            List<Long> identifiers = dates.keySet().stream().sorted().toList();
            String sql = "SELECT date_id, ticker_id, " + SqlIdentifiers.quote(column, column) + " FROM "
                    + SqlIdentifiers.quote(table, table) + " WHERE date_id IN ("
                    + String.join(",", Collections.nCopies(identifiers.size(), "?")) + ") ORDER BY date_id, ticker_id";
            try (PreparedStatement statement = connection.prepareStatement(sql, ResultSet.TYPE_FORWARD_ONLY, ResultSet.CONCUR_READ_ONLY)) {
                for (int index = 0; index < identifiers.size(); index++) statement.setLong(index + 1, identifiers.get(index));
                statement.setFetchSize(1000);
                try (ResultSet rows = statement.executeQuery()) {
                    while (rows.next()) {
                        long dateId = rows.getLong(1);
                        var value = rows.getBigDecimal(3);
                        consumer.accept(dates.get(dateId), rows.getLong(2), dateId, value == null ? null : value.doubleValue());
                    }
                }
            }
        }

        @Override
        public void close() throws SQLException {
            try { connection.rollback(); } finally { connection.close(); }
        }
    }

    @FunctionalInterface
    private interface RowConsumer {
        void accept(LocalDate date, long tickerId, long dateId, Double value);
    }
}
