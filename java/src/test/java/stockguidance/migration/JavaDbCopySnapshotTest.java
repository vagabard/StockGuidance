package stockguidance.migration;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.apache.fory.json.JsonObject;
import org.apache.fory.json.JsonArray;
import stockguidance.json.CanonicalJson;
import stockguidance.json.JsonSupport;

import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.Date;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

class JavaDbCopySnapshotTest {
    @TempDir Path directory;

    @Test
    void operationalDatabaseSnapshotWorksWithoutAnyArchiveFiles() throws Exception {
        List<LocalDate> dates = dates(2);
        JsonObject root = manifest(dates).root();
        root.remove("manifest_id");
        root.remove("files");
        root.put("schema_version", 2);
        root.put("kind", "daily-maintenance");
        root.put("input_mode", "database");
        root.put("source_key", "configured-source");
        root.put("data_root", directory.toString());
        root.put("archive_directory", directory.resolve("nonexistent-archives").toString());
        JsonArray inputs = JsonSupport.putArray(root, "input_sessions");
        JsonArray sources = JsonSupport.putArray(root, "database_sources");
        JsonObject availability = JsonSupport.putObject(root, "availability");
        for (LocalDate date : dates) {
            inputs.add(date.toString());
            JsonObject item = JsonSupport.addObject(sources);
            item.put("session", date.toString());
            item.put("raw_id", "id");
            availability.put(date.toString(), "receipt");
        }
        root.put("manifest_id", CanonicalJson.sha256(root));
        Path workload = directory.resolve("database-workload.json");
        Files.write(workload, CanonicalJson.bytes(root));
        JavaWriterManifest manifest = JavaWriterManifest.read(workload);
        assertTrue(manifest.databaseBacked());
        AtomicInteger queries = new AtomicInteger();
        Connection connection = (Connection) Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{Connection.class},
                (proxy, method, arguments) -> {
                    if (method.getName().equals("prepareStatement")) {
                        queries.incrementAndGet();
                        return statement(arguments[0].toString(), dates);
                    }
                    return null;
                });
        try (JavaDbCopy copy = JavaDbCopy.open(connection, manifest)) {
            var loaded = copy.get(dates);
            assertEquals("id", loaded.get(dates.getFirst()).evidence().rawId());
            assertTrue(loaded.get(dates.getFirst()).evidence().expected());
            assertEquals(100.25, loaded.get(dates.getFirst()).raw().get(1L).close());
            assertEquals(5, queries.get());
        }
    }

    @Test
    void jdbcSourceUsesOnlyConsistentReadsAndReusesRowsAcrossOverlappingWindows() throws Exception {
        List<LocalDate> dates = dates(2);
        JavaWriterManifest manifest = manifest(dates);
        List<String> queries = Collections.synchronizedList(new ArrayList<>());
        AtomicInteger rollbacks = new AtomicInteger();
        List<String> settings = new ArrayList<>();
        Connection connection = (Connection) Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{Connection.class},
                (proxy, method, arguments) -> {
                    switch (method.getName()) {
                        case "setReadOnly", "setAutoCommit", "setTransactionIsolation" -> settings.add(method.getName() + "=" + arguments[0]);
                        case "rollback" -> rollbacks.incrementAndGet();
                        case "commit" -> fail("Source connection must never commit writes");
                        case "prepareStatement" -> {
                            String sql = (String) arguments[0];
                            queries.add(sql);
                            assertTrue(sql.startsWith("SELECT "));
                            assertFalse(sql.contains("FOR UPDATE") && !sql.contains("`zscores_price_20`"));
                            assertFalse(sql.contains("FOR SHARE"));
                            return statement(sql, dates);
                        }
                        default -> { }
                    }
                    return null;
                });
        try (JavaDbCopy copy = JavaDbCopy.open(connection, manifest)) {
            var input = copy.get(dates);
            assertEquals(100.25, input.get(dates.getFirst()).raw().get(1L).close());
            assertNull(input.get(dates.getFirst()).deltas().get(1L).closeRatioToPrior());
            assertSame(input.get(dates.getLast()), copy.get(List.of(dates.getLast())).get(dates.getLast()));
            assertEquals(1, copy.batchCount());
            assertEquals(5, queries.size());
        }
        assertEquals(List.of("setTransactionIsolation=4", "setReadOnly=true", "setAutoCommit=false"), settings);
        assertEquals(1, rollbacks.get());
    }

    @Test
    void cachedParallelPreparationMatchesScalarReferenceForBothFamiliesAndWindows() throws Exception {
        List<LocalDate> dates = dates(61);
        JavaWriterManifest manifest = manifest(dates);
        Map<LocalDate, JavaDbCopy.SessionData> data = new HashMap<>();
        for (int index = 0; index < dates.size(); index++) {
            LocalDate date = dates.get(index);
            data.put(date, new JavaDbCopy.SessionData((long) index + 1,
                    new JavaZScoreCandidate.Evidence(true, "id", "id", "receipt"),
                    Map.of(1L, new JavaDbCopy.RawRow(1, index + 1, 100.0 + index), 2L, new JavaDbCopy.RawRow(2, index + 1, 42.0)),
                    Map.of(1L, new JavaDbCopy.DeltaRow(1, index + 1, index == 0 ? null : (100.0 + index) / (99.0 + index)),
                            2L, new JavaDbCopy.DeltaRow(2, index + 1, index == 0 ? null : 1.0))));
        }
        try (JavaDbCopy copy = new JavaDbCopy(manifest.tickerMappings(), batch -> {
            Map<LocalDate, JavaDbCopy.SessionData> result = new HashMap<>();
            for (LocalDate date : batch) result.put(date, data.get(date));
            return result;
        }); var calculators = Executors.newFixedThreadPool(3)) {
            List<java.util.concurrent.Future<JavaZScoreCandidate.PreparedSeries>> pending = new ArrayList<>();
            for (JavaZScoreCandidate.Family family : JavaZScoreCandidate.Family.values()) {
                for (int window : new int[]{20, 60}) {
                    pending.add(calculators.submit(() -> new JavaZScoreCandidate(manifest).prepare(copy, dates.getLast(), family, window)));
                }
            }
            for (var future : pending) {
                var prepared = future.get();
                var snapshot = prepared.snapshot();
                double expected;
                if (prepared.family() == JavaZScoreCandidate.Family.PRICE) {
                    expected = Math.sqrt(3.0 * (prepared.window() - 1) / (prepared.window() + 1));
                } else {
                    double[] values = new double[prepared.window()];
                    for (int index = 0; index < values.length; index++) {
                        int offset = 61 - values.length + index;
                        values[index] = (100.0 + offset) / (99.0 + offset) - 1;
                    }
                    double mean = java.util.Arrays.stream(values).average().orElseThrow();
                    double variance = java.util.Arrays.stream(values).map(value -> (value - mean) * (value - mean)).average().orElseThrow();
                    expected = (values[values.length - 1] - mean) / Math.sqrt(variance);
                }
                assertEquals(expected, snapshot.rows().getFirst().score(), 1e-6);
                assertEquals(JavaZScoreCandidate.Status.ZERO_VARIANCE, snapshot.rows().getLast().status());
                assertNull(snapshot.rows().getLast().score());
                assertEquals(1, snapshot.readyCount());
                var repeated = new JavaZScoreCandidate(manifest).prepare(copy, dates.getLast(), prepared.family(), prepared.window());
                assertEquals(snapshot, repeated.snapshot());
                assertThrows(UnsupportedOperationException.class, () -> snapshot.rows().clear());
                Path checkpoint = directory.resolve("stale.json");
                Files.writeString(checkpoint, "{\"schema_version\":1,\"manifest_id\":\"wrong\"}");
                assertThrows(IllegalArgumentException.class, () -> new JavaZScoreCandidate(manifest).writePrepared(
                        null, prepared, checkpoint, JavaWriterCandidate.InterruptPoint.NONE));
            }
            assertEquals(61, copy.sessionCount());
        }
    }

    @Test
    void preparedPublicationWritesOnlyOutputsAndPreservesCommitRecovery() throws Exception {
        List<LocalDate> dates = dates(61);
        JavaWriterManifest manifest = manifest(dates);
        var rows = List.of(new JavaZScoreCandidate.ScoreRow(1, 1.25f, JavaZScoreCandidate.Status.READY),
                new JavaZScoreCandidate.ScoreRow(2, null, JavaZScoreCandidate.Status.ZERO_VARIANCE));
        var snapshot = new JavaZScoreCandidate.Snapshot(rows, 1, "dependency", "availability", true, "content", 61L);
        var prepared = new JavaZScoreCandidate.PreparedSeries(dates.getLast(), JavaZScoreCandidate.Family.PRICE, 20, snapshot, Map.of());
        List<String> statements = new ArrayList<>();
        AtomicInteger commits = new AtomicInteger();
        AtomicInteger rollbacks = new AtomicInteger();
        java.util.concurrent.atomic.AtomicBoolean existingMatches = new java.util.concurrent.atomic.AtomicBoolean();
        Connection connection = (Connection) Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{Connection.class},
                (proxy, method, arguments) -> {
                    switch (method.getName()) {
                        case "getAutoCommit" -> { return true; }
                        case "getCatalog" -> { return "fixture_target"; }
                        case "commit" -> commits.incrementAndGet();
                        case "rollback" -> rollbacks.incrementAndGet();
                        case "prepareStatement" -> {
                            String sql = (String) arguments[0];
                            statements.add(sql);
                            assertFalse(sql.contains("FOR UPDATE") && !sql.contains("`zscores_price_20`"));
                            return Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{PreparedStatement.class},
                                    (statementProxy, action, parameters) -> {
                                        if (action.getName().equals("executeQuery")) {
                                            List<Object[]> values = new ArrayList<>();
                                            if (sql.contains("information_schema.TABLES")) {
                                                for (String table : manifest.tableNames().tables().values()) values.add(new Object[]{table, "InnoDB"});
                                                values.add(new Object[]{"stockguidance_migration_completion", "InnoDB"});
                                            } else if (sql.contains("information_schema.COLUMNS")) {
                                                for (String column : List.of("open", "high", "low", "close")) values.add(new Object[]{column, "decimal(28,8)"});
                                                values.add(new Object[]{"volume", "decimal(20,8)"});
                                                values.add(new Object[]{"vwap", "decimal(42,22)"});
                                            } else if (sql.contains("`stockguidance_zscore_series`")) {
                                                values.add(new Object[]{1, manifest.sourceScope(), manifest.calendarId(), "price", 20,
                                                        "rolling-population-zscore-v1", "not_applicable", "float32", "null_status_v1"});
                                            } else if (sql.contains("FROM `zscores_price_20`")) {
                                                if (existingMatches.get()) {
                                                    values.add(new Object[]{1L, 1.25f, 0});
                                                    values.add(new Object[]{2L, null, 5});
                                                }
                                            } else fail("Unexpected publication read: " + sql);
                                            return resultSet(values);
                                        }
                                        if (action.getName().equals("executeUpdate") || action.getName().equals("executeBatch")) {
                                            assertTrue(sql.startsWith("DELETE FROM `zscores_price_20`")
                                                    || sql.startsWith("INSERT INTO `zscores_price_20`")
                                                    || sql.startsWith("DELETE FROM `stockguidance_zscore_sessions`")
                                                    || sql.startsWith("INSERT INTO `stockguidance_zscore_sessions`"));
                                            return action.getName().equals("executeBatch") ? new int[]{1, 1} : 1;
                                        }
                                        return null;
                                    });
                        }
                        default -> { }
                    }
                    return null;
                });
        JavaZScoreCandidate candidate = new JavaZScoreCandidate(manifest, true, "fixture_target");
        Path checkpoint = directory.resolve("publication.json");
        assertThrows(JavaWriterCandidate.InterruptionException.class, () -> candidate.writePrepared(connection, prepared,
                checkpoint, JavaWriterCandidate.InterruptPoint.BEFORE_COMMIT));
        assertEquals(0, commits.get());
        assertEquals(1, rollbacks.get());
        assertFalse(Files.exists(checkpoint));
        assertThrows(JavaWriterCandidate.InterruptionException.class, () -> candidate.writePrepared(connection, prepared,
                checkpoint, JavaWriterCandidate.InterruptPoint.AFTER_COMMIT));
        assertEquals(1, commits.get());
        assertEquals(1, rollbacks.get());
        assertFalse(Files.exists(checkpoint));
        var result = candidate.writePrepared(connection, prepared, checkpoint, JavaWriterCandidate.InterruptPoint.NONE);
        assertEquals(2, commits.get());
        assertEquals(2, result.rowCount());
        assertTrue(Files.exists(checkpoint));
        assertEquals("content", JsonSupport.read(checkpoint).get("content_digest"));
        long materializationWrites = statements.stream().filter(sql -> sql.startsWith("DELETE FROM `zscores_price_20`") || sql.startsWith("INSERT INTO `zscores_price_20`")).count();
        existingMatches.set(true);
        candidate.writePrepared(connection, prepared, checkpoint, JavaWriterCandidate.InterruptPoint.NONE);
        assertEquals(materializationWrites, statements.stream().filter(sql -> sql.startsWith("DELETE FROM `zscores_price_20`") || sql.startsWith("INSERT INTO `zscores_price_20`")).count());
        assertEquals(2.0, candidate.phaseSeconds().get("preserved_output_rows"));
        assertTrue(statements.stream().noneMatch(sql -> sql.contains("FROM `raw_stock_data`") || sql.contains("FROM `daily_delta_stock_data`")));
    }

    private PreparedStatement statement(String sql, List<LocalDate> dates) {
        return (PreparedStatement) Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{PreparedStatement.class},
                (proxy, method, arguments) -> {
                    if (method.getName().equals("executeQuery")) {
                        List<Object[]> rows = new ArrayList<>();
                        if (sql.contains("`ticker_symbol`")) {
                            rows.add(new Object[]{1L, "AAP"});
                            rows.add(new Object[]{2L, "AAp"});
                        } else {
                            for (int index = 0; index < dates.size(); index++) {
                                if (sql.contains("`transaction_dates`")) rows.add(new Object[]{(long) index + 1, Date.valueOf(dates.get(index))});
                                if (sql.contains("`stockguidance_imports`")) rows.add(new Object[]{Date.valueOf(dates.get(index)), "id", "receipt"});
                                if (sql.contains("`raw_stock_data`")) rows.add(new Object[]{(long) index + 1, 1L, new BigDecimal("100.25")});
                                if (sql.contains("`daily_delta_stock_data`")) rows.add(new Object[]{(long) index + 1, 1L, null});
                            }
                        }
                        return resultSet(rows);
                    }
                    if (method.getName().startsWith("execute")) fail("Source query attempted a write");
                    return null;
                });
    }

    private ResultSet resultSet(List<Object[]> rows) {
        AtomicInteger position = new AtomicInteger(-1);
        java.util.concurrent.atomic.AtomicBoolean wasNull = new java.util.concurrent.atomic.AtomicBoolean();
        return (ResultSet) Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{ResultSet.class},
                (proxy, method, arguments) -> {
                    if (method.getName().equals("next")) return position.incrementAndGet() < rows.size();
                    if (method.getName().equals("wasNull")) return wasNull.get();
                    if (method.getName().startsWith("get")) {
                        Object value = rows.get(position.get())[(int) arguments[0] - 1];
                        wasNull.set(value == null);
                        return method.getName().equals("getFloat") && value == null ? 0.0f : value;
                    }
                    return null;
                });
    }

    private JavaWriterManifest manifest(List<LocalDate> dates) throws Exception {
        Path audit = directory.resolve("audit.json");
        Files.writeString(audit, "{}");
        JsonObject root = JsonSupport.object();
        root.put("schema_version", 1);
        root.put("stage", 6);
        root.put("calendar_id", "XNYS");
        root.put("corrected_schema_hash", "schema");
        JsonObject registry = JsonSupport.read(Path.of("../src/stockguidance/database/table_names.json"));
        root.put("authoritative_registry", registry);
        JsonObject inputs = JsonSupport.putObject(root, "inputs");
        inputs.put("data_root", directory.toString());
        inputs.put("audit_report", audit.toString());
        JsonSupport.putObject(root, "snapshot_identity").put("audit_sha256", java.util.HexFormat.of().formatHex(
                java.security.MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(audit))));
        JsonSupport.putObject(root, "source").put("archive_source", JsonSupport.object());
        JsonArray mappings = JsonSupport.putArray(JsonSupport.putObject(root, "mappings"), "ticker_mappings");
        JsonObject upper = JsonSupport.addObject(mappings);
        upper.put("id", 1); upper.put("label", "AAP");
        JsonObject lower = JsonSupport.addObject(mappings);
        lower.put("id", 2); lower.put("label", "AAp");
        JsonArray files = JsonSupport.putArray(root, "files");
        for (LocalDate date : dates) {
            JsonObject file = JsonSupport.addObject(files);
            file.put("session", date.toString()); file.put("valid", false);
        }
        JsonObject target = JsonSupport.addObject(JsonSupport.putArray(root, "targets"));
        target.put("session", dates.getLast().toString());
        target.put("derived_dependencies", JsonSupport.array());
        JsonArray dependencies = JsonSupport.putArray(target, "zscore_dependencies");
        dates.forEach(date -> dependencies.add(date.toString()));
        root.put("manifest_id", CanonicalJson.sha256(root));
        Path path = directory.resolve("manifest.json");
        Files.write(path, CanonicalJson.bytes(root));
        return JavaWriterManifest.read(path);
    }

    private static List<LocalDate> dates(int count) {
        List<LocalDate> dates = new ArrayList<>();
        for (int index = 0; index < count; index++) dates.add(LocalDate.of(2024, 1, 2).plusDays(index));
        return dates;
    }
}
