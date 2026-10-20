package stockguidance.migration;

import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.MySqlConnectionFactory;
import stockguidance.json.CanonicalJson;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.LocalDate;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

public final class JavaRawBenchmarkMain {
    private JavaRawBenchmarkMain() {
    }

    public static void main(String[] arguments) {
        try {
            System.out.print(new String(CanonicalJson.bytes(run(parse(arguments))), StandardCharsets.UTF_8));
        } catch (Exception error) {
            System.out.print("{\"failed\":true,\"error\":\"Java raw benchmark failed\",\"failure_type\":\""
                    + error.getClass().getSimpleName() + "\"}\n");
            System.exit(1);
        }
    }

    static ObjectNode run(Map<String, String> arguments) throws Exception {
        JavaWriterManifest manifest = JavaWriterManifest.read(Path.of(required(arguments, "manifest")));
        DatabaseConfiguration configuration = DatabaseConfiguration.load(Path.of(required(arguments, "dbinfo-file")));
        String schema = required(arguments, "disposable-schema");
        if (!schema.matches("stockguidance_disposable_[a-z0-9_]{1,39}")
                || schema.equalsIgnoreCase(configuration.database())) {
            throw new IllegalArgumentException("Require a distinct disposable MySQL schema");
        }
        configuration = new DatabaseConfiguration(configuration.host(), schema, configuration.user(),
                configuration.password(), configuration.port(), configuration.driver(), configuration.tableNamesFile());
        if (!configuration.loadTableNames().tables().equals(manifest.tableNames().tables())) {
            throw new IllegalArgumentException("Disposable database registry does not match the manifest");
        }
        int preparers = Integer.parseInt(required(arguments, "preparers"));
        if (preparers != 1 && preparers != 3) throw new IllegalArgumentException("Java preparers must be one or three");
        List<LocalDate> sessions = parseSessions(required(arguments, "sessions"));
        Path checkpointDirectory = Path.of(required(arguments, "checkpoint-directory"));
        Files.createDirectories(checkpointDirectory);
        JavaWriterCandidate candidate = new JavaWriterCandidate(manifest);
        ExecutorService executor = Executors.newFixedThreadPool(preparers);
        ArrayDeque<PendingArchive> pending = new ArrayDeque<>();
        Iterator<LocalDate> remaining = sessions.iterator();
        ArrayNode results = JsonNodeFactory.instance.arrayNode();
        long rows = 0;
        long changedRows = 0;
        long preparationStarted = System.nanoTime();
        long preparationFinished = preparationStarted;
        long writingStarted = 0;
        long writingFinished = 0;
        try (Connection connection = new MySqlConnectionFactory().open(configuration)) {
            candidate.validateTarget(connection);
            while (remaining.hasNext() || !pending.isEmpty()) {
                while (remaining.hasNext() && pending.size() < 3) {
                    LocalDate session = remaining.next();
                    pending.addLast(new PendingArchive(session, executor.submit(() -> manifest.archive(session))));
                }
                PendingArchive next = pending.removeFirst();
                ArchiveCandidateReader.SessionArchive archive = next.future().get();
                preparationFinished = System.nanoTime();
                if (writingStarted == 0) writingStarted = System.nanoTime();
                JavaWriterCandidate.WriteResult result = candidate.writeRaw(connection, archive,
                        checkpointDirectory.resolve(next.session() + ".json"), JavaWriterCandidate.InterruptPoint.NONE);
                writingFinished = System.nanoTime();
                rows += archive.bars().size();
                changedRows += result.changedRows();
                ObjectNode item = results.addObject();
                item.put("session", next.session().toString());
                item.put("rows", archive.bars().size());
                item.put("changed_rows", result.changedRows());
                item.put("status", result.status());
            }
        } finally {
            executor.shutdownNow();
        }
        ObjectNode report = JsonNodeFactory.instance.objectNode();
        report.put("schema_version", 1);
        report.put("backend", "java");
        report.put("preparers", preparers);
        report.put("status", "complete");
        report.put("rows", rows);
        report.put("changed_rows", changedRows);
        report.put("workload_seconds", (writingFinished - preparationStarted) / 1_000_000_000.0);
        report.put("preparation_wall_seconds", (preparationFinished - preparationStarted) / 1_000_000_000.0);
        report.put("write_wall_seconds", writingStarted == 0 ? 0 : (writingFinished - writingStarted) / 1_000_000_000.0);
        report.put("query_count", "unavailable: JDBC statement counter is not exposed by the candidate");
        report.put("transaction_unit", "one session");
        report.set("sessions", results);
        return report;
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

    private record PendingArchive(LocalDate session, Future<ArchiveCandidateReader.SessionArchive> future) {
    }
}
