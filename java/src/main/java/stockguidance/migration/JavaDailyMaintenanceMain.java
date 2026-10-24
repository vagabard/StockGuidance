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
