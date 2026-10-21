package stockguidance.migration;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.MySqlConnectionFactory;
import stockguidance.json.CanonicalJson;
import stockguidance.json.JsonSupport;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public final class JavaZScoreBenchmarkMain {
    private JavaZScoreBenchmarkMain() {
    }

    public static void main(String[] arguments) {
        try {
            System.out.print(new String(CanonicalJson.bytes(run(parse(arguments))), StandardCharsets.UTF_8));
        } catch (Exception error) {
            System.out.print("{\"failed\":true,\"error\":\"Java z-score benchmark failed\",\"failure_type\":\""
                    + error.getClass().getSimpleName() + "\"}\n");
            System.exit(1);
        }
    }

    static JsonObject run(Map<String, String> arguments) throws Exception {
        JavaWriterManifest manifest = JavaWriterManifest.read(Path.of(required(arguments, "manifest")));
        DatabaseConfiguration configuration = DatabaseConfiguration.load(Path.of(required(arguments, "dbinfo-file")));
        String schema = required(arguments, "disposable-schema");
        if (!schema.matches("stockguidance_disposable_[a-z0-9_]{1,39}")
                || schema.equalsIgnoreCase(configuration.database())) {
            throw new IllegalArgumentException("Require a distinct disposable MySQL schema");
        }
        configuration = new DatabaseConfiguration(configuration.host(), schema, configuration.user(),
                configuration.password(), configuration.port(), configuration.driver(), configuration.tableNamesFile());
        if (!configuration.loadTableNames().tables().equals(manifest.tableNames().tables())
                || !configuration.loadTableNames().templates().equals(manifest.tableNames().templates())) {
            throw new IllegalArgumentException("Disposable database registry does not match the manifest");
        }
        Path checkpointDirectory = Path.of(required(arguments, "checkpoint-directory"));
        Files.createDirectories(checkpointDirectory);
        List<LocalDate> sessions = parseSessions(required(arguments, "sessions"));
        java.util.Set<LocalDate> verifiedSources = new java.util.HashSet<>();
        JavaZScoreCandidate candidate = new JavaZScoreCandidate(manifest, verifiedSources);
        JsonArray series = JsonSupport.array();
        long rows = 0;
        long started = System.nanoTime();
        try (Connection connection = new MySqlConnectionFactory().open(configuration)) {
            for (LocalDate session : sessions) {
                for (JavaZScoreCandidate.Family family : JavaZScoreCandidate.Family.values()) {
                    for (int window : new int[]{20, 60}) {
                        long seriesStarted = System.nanoTime();
                        JavaZScoreCandidate.WriteResult result = candidate.write(
                                connection, session, family, window,
                                checkpointDirectory.resolve(session + "-" + familyValue(family) + "-" + window + ".json"),
                                JavaWriterCandidate.InterruptPoint.NONE);
                        rows += result.rowCount();
                        JsonObject item = JsonSupport.addObject(series);
                        item.put("session", session.toString());
                        item.put("family", familyValue(family));
                        item.put("window", window);
                        item.put("row_count", result.rowCount());
                        item.put("ready_count", result.readyCount());
                        item.put("content_digest", result.contentDigest());
                        item.put("seconds", (System.nanoTime() - seriesStarted) / 1_000_000_000.0);
                        JsonObject phases = JsonSupport.object();
                        candidate.phaseSeconds().forEach(phases::put);
                        item.put("phase_seconds", phases);
                        Path progress = checkpointDirectory.resolve("progress.tmp");
                        Files.write(progress, CanonicalJson.bytes(series));
                        Files.move(progress, checkpointDirectory.resolve("progress.json"),
                                java.nio.file.StandardCopyOption.ATOMIC_MOVE, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
                    }
                }
            }
        }
        JsonObject report = JsonSupport.object();
        report.put("schema_version", 1);
        report.put("backend", "java");
        report.put("preparers", 1);
        report.put("status", "complete");
        report.put("rows", rows);
        report.put("verified_archive_count", verifiedSources.stream().filter(manifest::isAdoptable).count());
        report.put("workload_seconds", (System.nanoTime() - started) / 1_000_000_000.0);
        report.put("query_count", "unavailable: JDBC statement counter is not exposed by the candidate");
        report.put("transaction_unit", "one z-score series/session");
        report.put("series", series);
        return report;
    }

    private static String familyValue(JavaZScoreCandidate.Family family) {
        return family == JavaZScoreCandidate.Family.PRICE ? "price" : "price_delta";
    }

    private static List<LocalDate> parseSessions(String value) {
        List<LocalDate> result = new ArrayList<>();
        for (String item : value.split(",")) {
            if (item.isBlank()) throw new IllegalArgumentException("Empty session");
            result.add(LocalDate.parse(item));
        }
        if (result.isEmpty()) throw new IllegalArgumentException("At least one session is required");
        return result;
    }

    private static Map<String, String> parse(String[] arguments) {
        Map<String, String> result = new HashMap<>();
        for (int index = 0; index < arguments.length; index++) {
            String argument = arguments[index];
            if (!argument.startsWith("--") || index + 1 >= arguments.length || arguments[index + 1].startsWith("--")) {
                throw new IllegalArgumentException("Benchmark arguments must be named pairs");
            }
            if (result.putIfAbsent(argument.substring(2), arguments[++index]) != null) {
                throw new IllegalArgumentException("Duplicate benchmark argument");
            }
        }
        return result;
    }

    private static String required(Map<String, String> arguments, String name) {
        String value = arguments.get(name);
        if (value == null || value.isBlank()) throw new IllegalArgumentException("Missing benchmark argument");
        return value;
    }
}
