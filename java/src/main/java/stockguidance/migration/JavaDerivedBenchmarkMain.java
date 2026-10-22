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
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

public final class JavaDerivedBenchmarkMain {
    private JavaDerivedBenchmarkMain() {
    }

    public static void main(String[] arguments) {
        try {
            System.out.print(new String(CanonicalJson.bytes(run(parse(arguments))), StandardCharsets.UTF_8));
        } catch (Exception error) {
            System.out.print("{\"failed\":true,\"error\":\"Java derived benchmark failed\",\"failure_type\":\""
                    + error.getClass().getSimpleName() + "\"}\n");
            System.exit(1);
        }
    }

    static JsonObject run(Map<String, String> arguments) throws Exception {
        JavaWriterManifest manifest = JavaWriterManifest.read(Path.of(required(arguments, "manifest")));
        DatabaseConfiguration configuration = DatabaseConfiguration.load(Path.of(required(arguments, "dbinfo-file")));
        boolean production = Boolean.parseBoolean(arguments.getOrDefault("production", "false"));
        String schema = production ? configuration.database() : required(arguments, "disposable-schema");
        if (!production && (!schema.matches("stockguidance_disposable_[a-z0-9_]{1,39}")
                || schema.equalsIgnoreCase(configuration.database()))) {
            throw new IllegalArgumentException("Require a distinct disposable MySQL schema");
        }
        if (!production) configuration = new DatabaseConfiguration(configuration.host(), schema, configuration.user(),
                configuration.password(), configuration.port(), configuration.driver(), configuration.tableNamesFile());
        if (!configuration.loadTableNames().tables().equals(manifest.tableNames().tables())) {
            throw new IllegalArgumentException("Disposable database registry does not match the manifest");
        }
        int preparers = Integer.parseInt(required(arguments, "preparers"));
        if (preparers != 1 && preparers != 3) throw new IllegalArgumentException("Java preparers must be one or three");
        List<LocalDate> sessions = parseSessions(required(arguments, "sessions"));
        Path checkpointDirectory = Path.of(required(arguments, "checkpoint-directory"));
        Files.createDirectories(checkpointDirectory);
        JavaWriterCandidate candidate = new JavaWriterCandidate(manifest, production, schema);
        ExecutorService executor = Executors.newFixedThreadPool(preparers);
        ArrayDeque<PendingDerived> pending = new ArrayDeque<>();
        Iterator<LocalDate> remaining = sessions.iterator();
        JsonArray results = JsonSupport.array();
        long rows = 0;
        long preparationStarted = System.nanoTime();
        long preparationFinished = preparationStarted;
        long writingStarted = 0;
        long writingFinished = 0;
        try (Connection connection = new MySqlConnectionFactory().open(configuration)) {
            candidate.validateTarget(connection);
            while (remaining.hasNext() || !pending.isEmpty()) {
                while (remaining.hasNext() && pending.size() < 3) {
                    LocalDate session = remaining.next();
                    pending.addLast(new PendingDerived(session, executor.submit(() -> prepare(manifest, session))));
                }
                PendingDerived next = pending.removeFirst();
                PreparedDerived prepared = next.future().get();
                preparationFinished = System.nanoTime();
                if (writingStarted == 0) writingStarted = System.nanoTime();
                JavaWriterCandidate.WriteResult result = candidate.writeDerived(connection, next.session(),
                        prepared.current(), prepared.prior(), prepared.following(),
                        checkpointDirectory.resolve(next.session() + ".json"), JavaWriterCandidate.InterruptPoint.NONE);
                writingFinished = System.nanoTime();
                rows += prepared.current().bars().size();
                JsonObject item = JsonSupport.addObject(results);
                item.put("session", next.session().toString());
                item.put("rows", prepared.current().bars().size());
                item.put("status", result.status());
                item.put("content_id", result.contentDigest());
            }
        } finally {
            executor.shutdownNow();
        }
        JsonObject report = JsonSupport.object();
        report.put("schema_version", 1);
        report.put("backend", "java");
        report.put("preparers", preparers);
        report.put("status", "complete");
        report.put("rows", rows);
        report.put("workload_seconds", (writingFinished - preparationStarted) / 1_000_000_000.0);
        report.put("preparation_wall_seconds", (preparationFinished - preparationStarted) / 1_000_000_000.0);
        report.put("write_wall_seconds", writingStarted == 0 ? 0 : (writingFinished - writingStarted) / 1_000_000_000.0);
        report.put("query_count", "unavailable: JDBC statement counter is not exposed by the candidate");
        report.put("transaction_unit", "one derived session");
        report.put("sessions", results);
        return report;
    }

    private static PreparedDerived prepare(JavaWriterManifest manifest, LocalDate session) {
        JavaWriterManifest.TargetEntry target = manifest.target(session);
        if (target.derivedDependencies().size() != 2) {
            throw new JavaWriterManifest.InvalidSourceException("Derived dependencies are unavailable: " + session);
        }
        LocalDate prior = target.derivedDependencies().get(0);
        LocalDate following = target.derivedDependencies().get(1);
        return new PreparedDerived(session, manifest.archive(session), manifest.archive(prior), manifest.archive(following));
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

    private record PendingDerived(LocalDate session, Future<PreparedDerived> future) {
    }

    private record PreparedDerived(LocalDate session, ArchiveCandidateReader.SessionArchive current,
                                   ArchiveCandidateReader.SessionArchive prior,
                                   ArchiveCandidateReader.SessionArchive following) {
    }
}
