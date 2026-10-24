package stockguidance.migration;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import stockguidance.json.CanonicalJson;
import stockguidance.json.JsonSupport;

import java.lang.reflect.Proxy;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class RawWriterPerformanceTest {
    @TempDir
    Path directory;

    @Test
    void batchesAreBoundedAndTickerMappingsAreReusedAfterCommit() throws Exception {
        Fixture fixture = fixture(2001);
        Database database = new Database(fixture.manifest());
        JavaWriterCandidate candidate = new JavaWriterCandidate(fixture.manifest());
        try (var writer = candidate.openExclusiveRawWriteSession(database.connection())) {
            writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE);
            writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE);
        }
        assertEquals(1, database.tickerReads);
        assertEquals(2, database.commits);
        assertEquals(2001, database.tickers.size());
        assertEquals(List.of(1000, 1000, 1), database.tickerBatches);
        assertEquals(List.of(1000, 1000, 1, 1000, 1000, 1), database.priceBatches);
    }

    @Test
    void rollbackDiscardsTentativeTickersAndReloadsTheMapping() throws Exception {
        Fixture fixture = fixture(2);
        Database database = new Database(fixture.manifest());
        JavaWriterCandidate candidate = new JavaWriterCandidate(fixture.manifest());
        try (var writer = candidate.openExclusiveRawWriteSession(database.connection())) {
            assertThrows(JavaWriterCandidate.InterruptionException.class, () ->
                    writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.BEFORE_COMMIT));
            assertTrue(database.tickers.isEmpty());
            writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE);
        }
        assertEquals(2, database.tickerReads);
        assertEquals(1, database.rollbacks);
        assertEquals(2, database.tickers.size());
        assertTrue(database.tickers.containsKey("AAP"));
        assertTrue(database.tickers.containsKey("AAp"));
        assertEquals(java.util.Set.of(1L, 2L), java.util.Set.copyOf(database.tickers.values()));
    }

    @Test
    void failedBatchAndAfterCommitInterruptionCanBeRetried() throws Exception {
        Fixture fixture = fixture(2);
        Database database = new Database(fixture.manifest());
        JavaWriterCandidate candidate = new JavaWriterCandidate(fixture.manifest());
        try (var writer = candidate.openExclusiveRawWriteSession(database.connection())) {
            database.failBatch = true;
            assertThrows(SQLException.class, () -> writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE));
            assertTrue(database.tickers.isEmpty());
            database.failBatch = false;
            assertThrows(JavaWriterCandidate.InterruptionException.class, () ->
                    writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.AFTER_COMMIT));
            assertEquals(2, database.tickers.size());
            writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE);
        }
        assertEquals(3, database.tickerReads);
        assertEquals(1, database.rollbacks);
        assertEquals(2, database.commits);
    }

    @Test
    void sessionsDoNotShareMappingsAndRejectCatalogChangesOrReuseAfterClose() throws Exception {
        Fixture fixture = fixture(2);
        JavaWriterCandidate candidate = new JavaWriterCandidate(fixture.manifest());
        Database first = new Database(fixture.manifest());
        Database second = new Database(fixture.manifest());
        var writer = candidate.openExclusiveRawWriteSession(first.connection());
        writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE);
        writer.close();
        assertThrows(IllegalStateException.class, () -> writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE));
        try (var other = candidate.openExclusiveRawWriteSession(second.connection())) {
            other.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE);
            second.catalog = "stockguidance_disposable_changed";
            assertThrows(IllegalStateException.class, () -> other.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE));
        }
        assertEquals(1, first.tickerReads);
        assertEquals(1, second.tickerReads);
        assertEquals(first.tickers, second.tickers);
    }

    @Test
    void updateBatchesAreBoundedAndPartialBatchFailureRollsBackTheSession() throws Exception {
        Fixture fixture = fixture(2001);
        Database database = new Database(fixture.manifest());
        JavaWriterCandidate candidate = new JavaWriterCandidate(fixture.manifest());
        try (var writer = candidate.openExclusiveRawWriteSession(database.connection())) {
            database.failPriceBatch = 2;
            assertThrows(SQLException.class, () -> writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE));
            assertTrue(database.tickers.isEmpty());
            assertEquals(1, database.rollbacks);
            assertEquals(0, database.commits);
            database.failPriceBatch = 0;
            writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE);
            database.storedBars = database.tickers.entrySet().stream()
                    .map(entry -> java.util.Arrays.<Object>asList(entry.getValue(), entry.getKey(), "10", "12", "9", "10", "100", null, null))
                    .toList();
            assertEquals(2001, writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE).changedRows());
        }
        assertEquals(List.of(1000, 1000, 1), database.updateBatches);
        assertEquals(2, database.tickerReads);
        assertEquals(2, database.commits);
    }

    @Test
    void laterSessionsExtendTheCommittedTickerMappingWithoutReloading() throws Exception {
        Fixture fixture = fixture(2);
        Path archivePath = directory.resolve("2024-07-02.ndjson");
        Files.writeString(archivePath, "{\"T\":\"NEW\",\"o\":10,\"h\":12,\"l\":9,\"c\":11,\"v\":100,\"t\":1719950400000}\n");
        var following = ArchiveCandidateReader.read(archivePath, LocalDate.of(2024, 7, 2));
        JsonObject root = fixture.manifest().root();
        JsonObject file = JsonSupport.addObject((JsonArray) root.get("files"));
        file.put("session", following.session().toString());
        file.put("path", archivePath.toString());
        file.put("valid", true);
        file.put("file_sha256", following.fileSha256());
        file.put("records_id", following.recordsId());
        root.remove("manifest_id");
        root.put("manifest_id", CanonicalJson.sha256(root));
        Files.write(fixture.manifest().manifestPath(), CanonicalJson.bytes(root));
        JavaWriterManifest manifest = JavaWriterManifest.read(fixture.manifest().manifestPath());
        Database database = new Database(manifest);
        try (var writer = new JavaWriterCandidate(manifest).openExclusiveRawWriteSession(database.connection())) {
            writer.writeRaw(fixture.archive(), null, JavaWriterCandidate.InterruptPoint.NONE);
            writer.writeRaw(following, null, JavaWriterCandidate.InterruptPoint.NONE);
            writer.writeRaw(following, null, JavaWriterCandidate.InterruptPoint.NONE);
        }
        assertEquals(1, database.tickerReads);
        assertEquals(3L, database.tickers.get("NEW"));
        assertEquals(List.of(2, 1), database.tickerBatches);
    }

    private Fixture fixture(int count) throws Exception {
        Path archivePath = directory.resolve("2024-07-01.ndjson");
        StringBuilder content = new StringBuilder();
        for (int index = 0; index < count; index++) {
            String ticker = index == 0 ? "AAP" : index == 1 ? "AAp" : "T" + index;
            content.append("{\"T\":\"").append(ticker)
                    .append("\",\"o\":10,\"h\":12,\"l\":9,\"c\":11,\"v\":100,\"t\":1719864000000}\n");
        }
        Files.writeString(archivePath, content);
        var archive = ArchiveCandidateReader.read(archivePath, LocalDate.of(2024, 7, 1));
        JsonObject root = JsonSupport.object();
        root.put("schema_version", 1);
        root.put("stage", 6);
        root.put("calendar_id", "XNYS");
        try (var input = getClass().getResourceAsStream("/stockguidance/database/table_names.json")) {
            root.put("authoritative_registry", JsonSupport.readObject(input));
        }
        root.put("corrected_schema_hash", "fixture-schema");
        Path audit = directory.resolve("audit.json");
        Files.writeString(audit, "{}");
        JsonObject inputs = JsonSupport.putObject(root, "inputs");
        inputs.put("data_root", directory.toString());
        inputs.put("audit_report", audit.toString());
        JsonSupport.putObject(root, "snapshot_identity").put("audit_sha256",
                HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(audit))));
        JsonObject file = JsonSupport.addObject(JsonSupport.putArray(root, "files"));
        file.put("session", archive.session().toString());
        file.put("path", archivePath.toString());
        file.put("valid", true);
        file.put("file_sha256", archive.fileSha256());
        file.put("records_id", archive.recordsId());
        JsonObject target = JsonSupport.addObject(JsonSupport.putArray(root, "targets"));
        target.put("session", archive.session().toString());
        target.put("derived_dependencies", JsonSupport.array());
        root.put("manifest_id", CanonicalJson.sha256(root));
        Path manifest = directory.resolve("manifest.json");
        Files.write(manifest, CanonicalJson.bytes(root));
        return new Fixture(JavaWriterManifest.read(manifest), archive);
    }

    private record Fixture(JavaWriterManifest manifest, ArchiveCandidateReader.SessionArchive archive) {
    }

    private static final class Database {
        private final JavaWriterManifest manifest;
        private final Map<String, Long> tickers = new LinkedHashMap<>();
        private final Map<String, Long> pendingTickers = new LinkedHashMap<>();
        private final List<Integer> tickerBatches = new ArrayList<>();
        private final List<Integer> priceBatches = new ArrayList<>();
        private final List<Integer> updateBatches = new ArrayList<>();
        private List<List<Object>> storedBars = List.of();
        private String catalog = "stockguidance_disposable_fixture";
        private boolean autoCommit = true;
        private boolean failBatch;
        private int failPriceBatch;
        private int tickerReads;
        private int commits;
        private int rollbacks;

        private Database(JavaWriterManifest manifest) {
            this.manifest = manifest;
        }

        private Connection connection() {
            return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[]{Connection.class},
                    (proxy, method, arguments) -> switch (method.getName()) {
                        case "getCatalog" -> catalog;
                        case "getAutoCommit" -> autoCommit;
                        case "setAutoCommit" -> { autoCommit = (Boolean) arguments[0]; yield null; }
                        case "prepareStatement" -> statement((String) arguments[0]);
                        case "commit" -> { tickers.putAll(pendingTickers); pendingTickers.clear(); commits++; yield null; }
                        case "rollback" -> { pendingTickers.clear(); rollbacks++; yield null; }
                        case "close" -> null;
                        default -> throw new UnsupportedOperationException(method.getName());
                    });
        }

        private PreparedStatement statement(String sql) {
            Map<Integer, Object> parameters = new HashMap<>();
            List<Map<Integer, Object>> batch = new ArrayList<>();
            return (PreparedStatement) Proxy.newProxyInstance(PreparedStatement.class.getClassLoader(), new Class<?>[]{PreparedStatement.class},
                    (proxy, method, arguments) -> switch (method.getName()) {
                        case "setString", "setLong", "setObject" -> { parameters.put((Integer) arguments[0], arguments[1]); yield null; }
                        case "setNull" -> { parameters.put((Integer) arguments[0], null); yield null; }
                        case "executeQuery" -> rows(query(sql));
                        case "executeUpdate" -> 1;
                        case "addBatch" -> { batch.add(new HashMap<>(parameters)); yield null; }
                        case "executeBatch" -> {
                            if (failBatch) throw new SQLException("Injected batch failure");
                            if (sql.startsWith("INSERT INTO `" + manifest.tableNames().tickers() + "`")) {
                                tickerBatches.add(batch.size());
                                for (var values : batch) pendingTickers.put((String) values.get(2), (Long) values.get(1));
                            } else if (sql.startsWith("INSERT INTO `" + manifest.tableNames().prices() + "`")) {
                                if (priceBatches.size() + 1 == failPriceBatch) throw new SQLException("Injected later batch failure");
                                priceBatches.add(batch.size());
                            } else if (sql.startsWith("UPDATE `" + manifest.tableNames().prices() + "`")) {
                                updateBatches.add(batch.size());
                            }
                            yield new int[batch.size()];
                        }
                        case "clearBatch" -> { batch.clear(); yield null; }
                        case "close" -> null;
                        default -> throw new UnsupportedOperationException(method.getName());
                    });
        }

        private List<List<Object>> query(String sql) {
            if (sql.startsWith("SELECT `key`, `value`")) {
                JsonArray objects = JsonSupport.array();
                objects.add(JsonSupport.array());
                objects.add(JsonSupport.array());
                Map<String, String> metadata = Map.of(
                        "production_target", "false", "database_schema", catalog, "database_engine", "mysql",
                        "corrected_schema_hash", manifest.correctedSchemaHash(),
                        "actual_schema_hash", CanonicalJson.sha256(JsonSupport.array()),
                        "actual_objects_hash", CanonicalJson.sha256(objects),
                        "registry", new String(CanonicalJson.bytes(((JsonObject) manifest.root().get("authoritative_registry")).get("tables")), java.nio.charset.StandardCharsets.UTF_8),
                        "schema_hash", "fixture-schema");
                return metadata.entrySet().stream().map(entry -> List.<Object>of(entry.getKey(), entry.getValue())).toList();
            }
            if (sql.startsWith("SELECT id, label")) {
                tickerReads++;
                return tickers.entrySet().stream().map(entry -> List.<Object>of(entry.getValue(), entry.getKey())).toList();
            }
            if (sql.startsWith("SELECT id FROM")) return List.of(List.of(1L));
            if (sql.startsWith("SELECT p.ticker_id")) return storedBars;
            return List.of();
        }

        private ResultSet rows(List<List<Object>> values) {
            int[] position = {-1};
            return (ResultSet) Proxy.newProxyInstance(ResultSet.class.getClassLoader(), new Class<?>[]{ResultSet.class},
                    (proxy, method, arguments) -> switch (method.getName()) {
                        case "next" -> ++position[0] < values.size();
                        case "getString" -> {
                            Object value = values.get(position[0]).get((Integer) arguments[0] - 1);
                            yield value == null ? null : value.toString();
                        }
                        case "getLong" -> ((Number) values.get(position[0]).get((Integer) arguments[0] - 1)).longValue();
                        case "wasNull" -> false;
                        case "close" -> null;
                        default -> throw new UnsupportedOperationException(method.getName());
                    });
        }
    }
}
