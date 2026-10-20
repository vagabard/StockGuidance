package stockguidance.migration;

import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.MySqlConnectionFactory;
import stockguidance.json.CanonicalJson;

import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.LocalDate;
import java.util.HashMap;
import java.util.Map;

public final class JavaZScoreMain {
    private JavaZScoreMain() {
    }

    public static void main(String[] arguments) {
        try {
            ObjectNode report = run(parse(arguments));
            System.out.print(new String(CanonicalJson.bytes(report), StandardCharsets.UTF_8));
        } catch (Exception error) {
            System.out.print("{\"failed\":true,\"error\":\"Java z-score candidate failed\",\"failure_type\":\""
                    + error.getClass().getSimpleName() + "\"}\n");
            System.exit(1);
        }
    }

    static ObjectNode run(Map<String, String> arguments) throws Exception {
        JavaWriterManifest manifest = JavaWriterManifest.read(Path.of(required(arguments, "manifest")));
        DatabaseConfiguration configuration = DatabaseConfiguration.load(Path.of(required(arguments, "dbinfo-file")));
        String schema = required(arguments, "disposable-schema");
        if (!schema.matches("stockguidance_disposable_[a-z0-9_]{1,39}") || schema.equalsIgnoreCase(configuration.database())) {
            throw new IllegalArgumentException("Require a distinct disposable MySQL schema");
        }
        configuration = new DatabaseConfiguration(configuration.host(), schema, configuration.user(),
                configuration.password(), configuration.port(), configuration.driver(), configuration.tableNamesFile());
        if (!configuration.loadTableNames().tables().equals(manifest.tableNames().tables())
                || !configuration.loadTableNames().templates().equals(manifest.tableNames().templates())) {
            throw new IllegalArgumentException("Disposable database registry does not match the manifest");
        }
        JavaZScoreCandidate.Family family = JavaZScoreCandidate.Family.parse(required(arguments, "family"));
        int window = Integer.parseInt(required(arguments, "window"));
        LocalDate session = LocalDate.parse(required(arguments, "session"));
        Path checkpoint = arguments.containsKey("checkpoint") ? Path.of(arguments.get("checkpoint")) : null;
        JavaWriterCandidate.InterruptPoint interruption = JavaWriterCandidate.InterruptPoint.valueOf(
                arguments.getOrDefault("interrupt", "none").toUpperCase());
        JavaZScoreCandidate candidate = new JavaZScoreCandidate(manifest);
        JavaZScoreCandidate.WriteResult result;
        try (Connection connection = new MySqlConnectionFactory().open(configuration)) {
            result = candidate.write(connection, session, family, window, checkpoint, interruption);
        }
        ObjectNode report = JsonNodeFactory.instance.objectNode();
        report.put("schema_version", 1);
        report.put("backend", "java");
        report.put("candidate", "jdbc-zscores");
        report.put("manifest_id", manifest.manifestId());
        report.put("family", result.family());
        report.put("window", result.window());
        report.put("session", result.session().toString());
        report.put("status", result.status());
        report.put("committed", result.committed());
        report.put("row_count", result.rowCount());
        report.put("ready_count", result.readyCount());
        report.put("content_digest", result.contentDigest());
        report.put("dependency_digest", result.dependencyDigest());
        report.put("availability_digest", result.availabilityDigest());
        report.put("actual_input_available", result.actualInputAvailable());
        report.put("production_access", false);
        return report;
    }

    private static Map<String, String> parse(String[] arguments) {
        Map<String, String> result = new HashMap<>();
        for (int index = 0; index < arguments.length; index++) {
            String argument = arguments[index];
            if (!argument.startsWith("--") || index + 1 >= arguments.length || arguments[index + 1].startsWith("--")) {
                throw new IllegalArgumentException("Z-score arguments must be named pairs");
            }
            if (result.putIfAbsent(argument.substring(2), arguments[++index]) != null) {
                throw new IllegalArgumentException("Duplicate z-score argument");
            }
        }
        return result;
    }

    private static String required(Map<String, String> arguments, String name) {
        String value = arguments.get(name);
        if (value == null || value.isBlank()) throw new IllegalArgumentException("Missing z-score argument");
        return value;
    }
}
