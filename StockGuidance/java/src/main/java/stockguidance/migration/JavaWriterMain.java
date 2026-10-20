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

public final class JavaWriterMain {
    private JavaWriterMain() {
    }

    public static void main(String[] arguments) {
        try {
            ObjectNode report = run(parse(arguments));
            System.out.print(new String(CanonicalJson.bytes(report), StandardCharsets.UTF_8));
        } catch (Exception error) {
            System.out.print("{\"failed\":true,\"error\":\"Java writer failed\"}\n");
            System.exit(1);
        }
    }

    static ObjectNode run(Map<String, String> arguments) throws Exception {
        JavaWriterManifest manifest = JavaWriterManifest.read(Path.of(required(arguments, "manifest")));
        DatabaseConfiguration configuration = DatabaseConfiguration.load(Path.of(required(arguments, "dbinfo-file")));
        if (!configuration.loadTableNames().tables().equals(manifest.tableNames().tables())) {
            throw new IllegalArgumentException("Disposable database registry does not match the manifest");
        }
        String workload = required(arguments, "workload");
        if (!workload.equals("raw") && !workload.equals("derived")) throw new IllegalArgumentException("Unsupported Java writer workload");
        LocalDate session = LocalDate.parse(required(arguments, "session"));
        Path checkpoint = arguments.containsKey("checkpoint") ? Path.of(arguments.get("checkpoint")) : null;
        JavaWriterCandidate.InterruptPoint interruption = JavaWriterCandidate.InterruptPoint.valueOf(arguments.getOrDefault("interrupt", "none").toUpperCase());
        JavaWriterCandidate candidate = new JavaWriterCandidate(manifest);
        JavaWriterCandidate.WriteResult result;
        try (Connection connection = new MySqlConnectionFactory().open(configuration)) {
            result = workload.equals("raw")
                    ? candidate.writeRaw(connection, session, checkpoint, interruption)
                    : candidate.writeDerived(connection, session, checkpoint, interruption);
        }
        ObjectNode report = JsonNodeFactory.instance.objectNode();
        report.put("schema_version", 1);
        report.put("backend", "java");
        report.put("candidate", "jdbc-writer");
        report.put("manifest_id", manifest.manifestId());
        report.put("workload", result.workload());
        report.put("session", result.session().toString());
        report.put("status", result.status());
        report.put("committed", result.committed());
        report.put("changed_rows", result.changedRows());
        if (result.contentDigest() == null) report.putNull("content_digest"); else report.put("content_digest", result.contentDigest());
        report.put("production_access", false);
        return report;
    }

    private static Map<String, String> parse(String[] arguments) {
        Map<String, String> result = new HashMap<>();
        for (int index = 0; index < arguments.length; index++) {
            String argument = arguments[index];
            if (!argument.startsWith("--") || index + 1 >= arguments.length || arguments[index + 1].startsWith("--")) {
                throw new IllegalArgumentException("Writer arguments must be named pairs");
            }
            if (result.putIfAbsent(argument.substring(2), arguments[++index]) != null) {
                throw new IllegalArgumentException("Duplicate writer argument");
            }
        }
        return result;
    }

    private static String required(Map<String, String> arguments, String name) {
        String value = arguments.get(name);
        if (value == null || value.isBlank()) throw new IllegalArgumentException("Missing writer argument");
        return value;
    }
}
