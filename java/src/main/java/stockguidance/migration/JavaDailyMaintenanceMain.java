package stockguidance.migration;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.MySqlConnectionFactory;
import stockguidance.database.SqlIdentifiers;
import stockguidance.json.CanonicalJson;
import stockguidance.json.JsonSupport;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public final class JavaDailyMaintenanceMain {
    private JavaDailyMaintenanceMain() {
    }

    public static void main(String[] arguments) {
        try {
            Map<String, String> options = new HashMap<>();
            for (int index = 0; index < arguments.length; index += 2) {
                if (!arguments[index].startsWith("--") || index + 1 == arguments.length
                        || options.putIfAbsent(arguments[index].substring(2), arguments[index + 1]) != null) {
                    throw new IllegalArgumentException("Expected unique named argument pairs");
                }
            }
            System.out.println(new String(CanonicalJson.bytes(run(options)), StandardCharsets.UTF_8));
        } catch (Exception error) {
            error.printStackTrace(System.err);
            System.out.println("{\"failed\":true,\"status\":\"failed\",\"error\":\"Java daily maintenance failed; see stderr\"}");
            System.exit(1);
        }
    }

    static JsonObject run(Map<String, String> options) throws Exception {
        Path workload = Path.of(options.get("workload"));
        JavaWriterManifest manifest = JavaWriterManifest.read(workload);
        if (!manifest.operational()) throw new IllegalArgumentException("Daily workload is required");
        DatabaseConfiguration configuration = DatabaseConfiguration.load(Path.of(options.get("dbinfo-file")));
        if (!configuration.loadTableNames().tables().equals(manifest.tableNames().tables())
                || !configuration.loadTableNames().templates().equals(manifest.tableNames().templates())) {
            throw new IllegalArgumentException("Daily workload registry differs from configured database");
        }
        if (manifest.databaseBacked()) return runDatabase(options, manifest, configuration);
        JsonObject root = manifest.root();
        List<LocalDate> rawDates = dates(root, "raw_sessions");
        List<LocalDate> derivedDates = dates(root, "derived_sessions");
        List<LocalDate> targets = dates(root, "zscore_sessions");
        Set<LocalDate> changedRaw = new HashSet<>();
        Set<LocalDate> changedDerived = new HashSet<>();
        List<LocalDate> zscoreDates = new ArrayList<>();
        try (Connection connection = new MySqlConnectionFactory().open(configuration)) {
            try (PreparedStatement statement = connection.prepareStatement("SELECT configuration_key, source_key FROM "
                    + SqlIdentifiers.quote(manifest.tableNames().source(), "source") + " WHERE id = 1");
                 ResultSet rows = statement.executeQuery()) {
                if (!rows.next() || !manifest.sourceKey().equals(rows.getString(1)) || !manifest.sourceKey().equals(rows.getString(2))) {
                    throw new IllegalArgumentException("Daily workload data configuration differs from database");
                }
            }
            JavaWriterCandidate writer = new JavaWriterCandidate(manifest, true, configuration.database());
            writer.validateTarget(connection);
            int completed = 0;
            for (LocalDate session : rawDates) {
                markRawImportStarted(connection, session);
                var result = writer.writeRaw(connection, session, null, JavaWriterCandidate.InterruptPoint.NONE);
                if (!result.status().equals("complete") && !result.status().equals("reused")) {
                    throw new IllegalStateException("Raw import did not complete for " + session + ": " + result.status());
                }
                clearRawImportStarted(connection, session);
                if (!result.status().equals("reused")) changedRaw.add(session);
                System.err.println("Raw " + (++completed) + "/" + rawDates.size() + " " + session + " " + result.status());
            }
            completed = 0;
            for (LocalDate session : derivedDates) {
                var neighbors = manifest.target(session).derivedDependencies();
                var prior = manifest.file(neighbors.get(0)).valid() ? manifest.archive(neighbors.get(0)) : empty(neighbors.get(0));
                var following = manifest.file(neighbors.get(1)).valid() ? manifest.archive(neighbors.get(1)) : empty(neighbors.get(1));
                var result = writer.writeDerived(connection, session, manifest.archive(session), prior, following,
                        null, JavaWriterCandidate.InterruptPoint.NONE);
                if (!result.status().equals("reused")) changedDerived.add(session);
                System.err.println("Derived " + (++completed) + "/" + derivedDates.size() + " " + session + " " + result.status());
            }
            JsonArray mappings = JsonSupport.array();
            try (PreparedStatement statement = connection.prepareStatement("SELECT id, label FROM "
                    + SqlIdentifiers.quote(manifest.tableNames().tickers(), "tickers") + " ORDER BY id");
                 ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    JsonObject item = JsonSupport.addObject(mappings);
                    item.put("id", rows.getLong(1));
                    item.put("label", rows.getString(2));
                }
            }
            JsonObject mappingObject = JsonSupport.object();
            mappingObject.put("ticker_mappings", mappings);
            root.put("mappings", mappingObject);
            for (LocalDate session : targets) {
                zscoreDates.add(session);
            }
            if (!connection.getAutoCommit()) connection.rollback();
        }
        root.remove("manifest_id");
        root.put("manifest_id", CanonicalJson.sha256(root));
        Path pipelineWorkload = workload.resolveSibling("pipeline-" + root.get("manifest_id") + ".json");
        Files.write(pipelineWorkload, CanonicalJson.bytes(root));
        JsonObject zscores = JsonSupport.object();
        zscores.put("status", "complete");
        zscores.put("sessions", 0);
        if (!zscoreDates.isEmpty()) {
            Map<String, String> pipelineOptions = new HashMap<>(options);
            pipelineOptions.put("manifest", pipelineWorkload.toString());
            pipelineOptions.put("production", "true");
            pipelineOptions.put("checkpoint-directory", Path.of(options.get("checkpoint-directory"))
                    .resolve(root.get("manifest_id").toString()).toString());
            try (JavaZScorePipelineMain pipeline = new JavaZScorePipelineMain(pipelineOptions)) {
                zscores = pipeline.run(zscoreDates);
            }
        }
        JsonObject report = JsonSupport.object();
        report.put("status", "complete");
        report.put("backend", "java");
        report.put("raw_changed", changedRaw.size());
        report.put("derived_changed", changedDerived.size());
        report.put("zscore_sessions", zscoreDates.size());
        report.put("zscores", zscores);
        return report;
    }

    private static ArchiveCandidateReader.SessionArchive empty(LocalDate date) {
        return new ArchiveCandidateReader.SessionArchive(null, date, null, Map.of(), ArchiveCandidateReader.recordsId(Map.of()));
    }

    private static JsonObject runDatabase(Map<String, String> options, JavaWriterManifest manifest,
                                          DatabaseConfiguration configuration) throws Exception {
        long started = System.nanoTime();
        JsonObject root = manifest.root();
        List<LocalDate> inputs = dates(root, "input_sessions");
        List<LocalDate> derivedDates = dates(root, "derived_sessions");
        List<LocalDate> targets = dates(root, "zscore_sessions");
        Set<LocalDate> refreshed = new HashSet<>(dates(root, "refreshed_sessions"));
        int rawChanged = 0;
        int derivedChanged = 0;
        int archiveReads = 0;
        try (Connection connection = new MySqlConnectionFactory().open(configuration)) {
            try (PreparedStatement statement = connection.prepareStatement("SELECT configuration_key, source_key FROM "
                    + SqlIdentifiers.quote(manifest.tableNames().source(), "source") + " WHERE id = 1");
                 ResultSet rows = statement.executeQuery()) {
                if (!rows.next() || !manifest.sourceKey().equals(rows.getString(1)) || !manifest.sourceKey().equals(rows.getString(2))) {
                    throw new IllegalArgumentException("Daily workload data configuration differs from database");
                }
            }
            OperationalDatabase.requireNoPartialImports(connection, inputs);
            var receipts = OperationalDatabase.receipts(connection, manifest.tableNames(), inputs);
            var fingerprints = OperationalDatabase.fingerprints(connection, manifest.sourceKey());
            JavaWriterCandidate writer = new JavaWriterCandidate(manifest, true, configuration.database());
            writer.validateTarget(connection);
            System.err.println("Database preparation: " + receipts.size() + "/" + inputs.size() + " raw sessions completed");
            List<LocalDate> missing = new ArrayList<>();
            for (LocalDate session : inputs) {
                var receipt = receipts.get(session);
                Path path = manifest.file(session).path();
                var fingerprint = path != null && Files.isRegularFile(path) ? OperationalDatabase.Fingerprint.read(path) : null;
                var previous = fingerprints.get(session);
                var action = OperationalDatabase.planInput(receipt, previous == null ? null : previous.fingerprint(), fingerprint,
                        refreshed.contains(session) || (previous != null && previous.pending()));
                if (action == OperationalDatabase.InputAction.MISSING) {
                    missing.add(session);
                    continue;
                }
                if (action == OperationalDatabase.InputAction.TRUSTED) continue;
                if (action == OperationalDatabase.InputAction.BASELINE) {
                    OperationalDatabase.saveFingerprint(connection, manifest.sourceKey(), session, fingerprint);
                    continue;
                }
                var archive = ArchiveCandidateReader.readForImport(path, session);
                archiveReads++;
                if (!fingerprint.equals(OperationalDatabase.Fingerprint.read(path))) {
                    throw new IllegalArgumentException("Source changed while reading: " + session);
                }
                if (receipt != null && receipt.rawId().equals(archive.recordsId()) && archive.badRecords().isEmpty()) {
                    OperationalDatabase.saveFingerprint(connection, manifest.sourceKey(), session, fingerprint);
                    continue;
                }
                String available = java.time.Instant.ofEpochSecond(Math.floorDiv(fingerprint.modifiedNs(), 1_000_000_000L),
                        Math.floorMod(fingerprint.modifiedNs(), 1_000_000_000L)).toString();
                manifest.availableAt(session, available);
                markRawImportStarted(connection, session);
                writer.writePendingRaw(connection, archive, fingerprint);
                rawChanged++;
                System.err.println("Imported raw " + session);
            }
            if (!missing.isEmpty()) throw new IllegalArgumentException("Missing raw sessions " + missing + "; run --stage source first");
            if (rawChanged > 0 || Boolean.TRUE.equals(manifest.root().get("scan_quality"))
                    || receipts.values().stream().anyMatch(receipt -> receipt.dependencyId() == null)) {
                long qualityStarted = System.nanoTime();
                RowQuality.reconcile(connection, manifest, inputs, OperationalDatabase.receipts(connection, manifest.tableNames(), inputs));
                connection.commit();
                System.err.printf("Classify and interpolate: %.3fs%n", (System.nanoTime() - qualityStarted) / 1e9);
            }
            receipts = OperationalDatabase.receipts(connection, manifest.tableNames(), inputs);
            manifest.databaseSources(receipts);
            List<LocalDate> pending = new ArrayList<>();
            for (LocalDate session : derivedDates) {
                var receipt = receipts.get(session);
                if (receipt == null) throw new IllegalArgumentException("Missing current raw receipt: " + session);
                String dependency = OperationalDatabase.dependency(session, manifest, receipts);
                if (!dependency.equals(receipt.dependencyId()) || receipt.derivedId() == null) pending.add(session);
            }
            for (List<LocalDate> batch : OperationalDatabase.batches(pending, 20)) {
                Set<LocalDate> required = new java.util.TreeSet<>(batch);
                for (LocalDate session : batch) required.addAll(manifest.target(session).derivedDependencies());
                var snapshots = OperationalDatabase.readRaw(connection, manifest.tableNames(), List.copyOf(required), receipts);
                for (LocalDate session : batch) {
                    var neighbors = manifest.target(session).derivedDependencies();
                    writer.writeDerived(connection, session, snapshots.get(session), snapshots.get(neighbors.get(0)),
                            snapshots.get(neighbors.get(1)), null, JavaWriterCandidate.InterruptPoint.NONE);
                    derivedChanged++;
                    System.err.println("Derived " + derivedChanged + "/" + pending.size() + " " + session);
                }
            }
            JsonArray mappings = JsonSupport.array();
            try (PreparedStatement statement = connection.prepareStatement("SELECT id, label FROM "
                    + SqlIdentifiers.quote(manifest.tableNames().tickers(), "tickers") + " ORDER BY id");
                 ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    JsonObject item = JsonSupport.addObject(mappings);
                    item.put("id", rows.getLong(1));
                    item.put("label", rows.getString(2));
                }
            }
            root = manifest.root();
            JsonObject mappingObject = JsonSupport.object();
            mappingObject.put("ticker_mappings", mappings);
            root.put("mappings", mappingObject);
            if (!connection.getAutoCommit()) connection.rollback();
        }
        root.remove("manifest_id");
        root.put("manifest_id", CanonicalJson.sha256(root));
        Path workload = Path.of(options.get("workload"));
        Path pipelineWorkload = workload.resolveSibling("pipeline-" + root.get("manifest_id") + ".json");
        Files.write(pipelineWorkload, CanonicalJson.bytes(root));
        Map<String, String> pipelineOptions = new HashMap<>(options);
        pipelineOptions.put("manifest", pipelineWorkload.toString());
        pipelineOptions.put("production", "true");
        pipelineOptions.put("checkpoint-directory", Path.of(options.get("checkpoint-directory")).resolve(root.get("manifest_id").toString()).toString());
        JsonObject zscores;
        try (JavaZScorePipelineMain pipeline = new JavaZScorePipelineMain(pipelineOptions)) {
            zscores = pipeline.run(targets);
        }
        JsonObject report = JsonSupport.object();
        report.put("status", "complete");
        report.put("backend", "java");
        report.put("raw_changed", rawChanged);
        report.put("derived_changed", derivedChanged);
        report.put("archive_reads", archiveReads);
        report.put("zscore_sessions", targets.size());
        report.put("zscores", zscores);
        report.put("elapsed_seconds", (System.nanoTime() - started) / 1e9);
        return report;
    }

    private static void markRawImportStarted(Connection connection, LocalDate session) throws Exception {
        boolean originalAutoCommit = connection.getAutoCommit();
        String sql = "INSERT INTO stockguidance_daily_import_in_progress (session_date, started_at) VALUES (?, ?) "
                + "ON DUPLICATE KEY UPDATE started_at = VALUES(started_at)";
        try {
            if (originalAutoCommit) connection.setAutoCommit(false);
            try (PreparedStatement statement = connection.prepareStatement(sql)) {
                statement.setDate(1, java.sql.Date.valueOf(session));
                statement.setString(2, java.time.Instant.now().toString());
                statement.executeUpdate();
            }
            connection.commit();
        } finally {
            if (originalAutoCommit) connection.setAutoCommit(true);
        }
    }

    private static void clearRawImportStarted(Connection connection, LocalDate session) throws Exception {
        boolean originalAutoCommit = connection.getAutoCommit();
        try {
            if (originalAutoCommit) connection.setAutoCommit(false);
            try (PreparedStatement statement = connection.prepareStatement(
                    "DELETE FROM stockguidance_daily_import_in_progress WHERE session_date = ?")) {
                statement.setDate(1, java.sql.Date.valueOf(session));
                statement.executeUpdate();
            }
            connection.commit();
        } finally {
            if (originalAutoCommit) connection.setAutoCommit(true);
        }
    }

    private static List<LocalDate> dates(JsonObject root, String key) {
        if (!(root.get(key) instanceof JsonArray values)) throw new IllegalArgumentException("Missing daily sessions: " + key);
        return values.stream().map(value -> LocalDate.parse(value.toString())).toList();
    }
}
