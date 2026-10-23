package stockguidance.migration;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.MySqlConnectionFactory;
import stockguidance.json.CanonicalJson;
import stockguidance.json.JsonSupport;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.sql.Connection;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletionService;
import java.util.concurrent.ExecutorCompletionService;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;

public final class JavaZScorePipelineMain implements AutoCloseable {
    private final JavaWriterManifest manifest;
    private final DatabaseConfiguration configuration;
    private final boolean production;
    private final Path checkpoints;
    private final JavaDbCopy source;
    private final java.util.concurrent.ThreadPoolExecutor calculators;
    private final ExecutorService publishers;
    private final Semaphore publicationSlots;
    private int workers;
    private final int writers;
    private final JsonArray progress = JsonSupport.array();

    private JavaZScorePipelineMain(Map<String, String> arguments) throws Exception {
        manifest = JavaWriterManifest.read(Path.of(required(arguments, "manifest")));
        configuration = targetConfiguration(arguments);
        production = Boolean.parseBoolean(arguments.getOrDefault("production", "false"));
        workers = workerCount(arguments, "workers", 3);
        writers = workerCount(arguments, "writers", 1);
        if (!configuration.loadTableNames().tables().equals(manifest.tableNames().tables())
                || !configuration.loadTableNames().templates().equals(manifest.tableNames().templates())) {
            throw new IllegalArgumentException("Registry differs from manifest");
        }
        checkpoints = Path.of(required(arguments, "checkpoint-directory"));
        Files.createDirectories(checkpoints);
        try (Connection connection = connect()) {
            candidate().validateSeries(connection);
        }
        source = JavaDbCopy.open(connect(), manifest);
        calculators = (java.util.concurrent.ThreadPoolExecutor) Executors.newFixedThreadPool(workers);
        publishers = Executors.newFixedThreadPool(writers);
        publicationSlots = new Semaphore(writers + 12);
    }

    public static void main(String[] arguments) {
        try {
            Map<String, String> options = parse(arguments);
            if (options.containsKey("parent-pid")) {
                ProcessHandle parent = ProcessHandle.of(Long.parseLong(options.get("parent-pid")))
                        .orElseThrow(() -> new IllegalArgumentException("Coordinator process is unavailable"));
                parent.onExit().thenRun(() -> System.exit(1));
            }
            try (JavaZScorePipelineMain pipeline = new JavaZScorePipelineMain(options)) {
                if (Boolean.parseBoolean(options.getOrDefault("serve", "false"))) {
                    try (BufferedReader reader = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
                        String line;
                        while ((line = reader.readLine()) != null) {
                            if (line.equals("close")) break;
                            if (line.startsWith("tune:")) print(pipeline.tune(parseSessions(line.substring(5))));
                            else print(pipeline.run(parseSessions(line)));
                        }
                    }
                } else {
                    print(pipeline.run(parseSessions(required(options, "sessions"))));
                }
            }
        } catch (Exception error) {
            System.out.println("{\"failed\":true,\"error\":\"Java z-score pipeline failed\",\"failure_type\":\""
                    + error.getClass().getSimpleName() + "\"}");
            System.exit(1);
        }
    }

    private JsonObject run(List<LocalDate> sessions) throws Exception {
        long started = System.nanoTime();
        synchronized (progress) { progress.clear(); }
        List<LocalDate> dependencies = sessions.stream().flatMap(session -> manifest.target(session).zscoreDependencies().stream())
                .distinct().sorted().toList();
        source.request(dependencies);
        CompletionService<List<JsonObject>> completion = new ExecutorCompletionService<>(calculators);
        int submitted = 0;
        int completed = 0;
        List<JsonObject> results = new ArrayList<>();
        while (completed < sessions.size()) {
            while (submitted < sessions.size() && submitted - completed < workers + 3) {
                LocalDate session = sessions.get(submitted++);
                completion.submit(() -> calculateSession(session));
            }
            results.addAll(completion.take().get());
            completed++;
        }
        results.sort(java.util.Comparator.comparing(value -> value.get("session") + "/" + value.get("family") + "/" + value.get("window")));
        JsonArray series = JsonSupport.array();
        series.addAll(results);
        JsonObject report = JsonSupport.object();
        report.put("schema_version", 1);
        report.put("backend", "java");
        report.put("pipeline", "shared_java_db_copy_v1");
        report.put("preparers", workers);
        report.put("writers", writers);
        report.put("status", "complete");
        report.put("rows", results.stream().mapToLong(value -> JsonSupport.longIntegerValue(value.get("row_count"))).sum());
        report.put("series", series);
        report.put("cached_sessions", source.sessionCount());
        report.put("source_batches", source.batchCount());
        report.put("source_snapshot", "read-only repeatable-read; no locking source reads");
        report.put("workload_seconds", (System.nanoTime() - started) / 1e9);
        report.put("transaction_unit", "one z-score series/session");
        return report;
    }

    private List<JsonObject> calculateSession(LocalDate session) throws Exception {
        List<Future<JsonObject>> pending = new ArrayList<>();
        for (JavaZScoreCandidate.Family family : JavaZScoreCandidate.Family.values()) {
            for (int window : new int[]{20, 60}) {
                JavaZScoreCandidate calculator = candidate();
                long started = System.nanoTime();
                var prepared = calculator.prepare(source, session, family, window);
                double preparationSeconds = (System.nanoTime() - started) / 1e9;
                publicationSlots.acquire();
                try {
                    pending.add(publishers.submit(() -> {
                        try {
                            return publish(prepared, preparationSeconds);
                        } finally {
                            publicationSlots.release();
                        }
                    }));
                } catch (RuntimeException error) {
                    publicationSlots.release();
                    throw error;
                }
            }
        }
        List<JsonObject> results = new ArrayList<>();
        for (Future<JsonObject> result : pending) results.add(result.get());
        return results;
    }

    private JsonObject publish(JavaZScoreCandidate.PreparedSeries prepared, double preparationSeconds) throws Exception {
        long started = System.nanoTime();
        String family = prepared.family() == JavaZScoreCandidate.Family.PRICE ? "price" : "price_delta";
        Path checkpoint = checkpoints.resolve(prepared.session() + "-" + family + "-" + prepared.window() + ".json");
        java.util.concurrent.atomic.AtomicLong preserved = new java.util.concurrent.atomic.AtomicLong();
        JavaZScoreCandidate.WriteResult result = retryPublication(() -> {
            try (Connection connection = connect()) {
                JavaZScoreCandidate writer = candidate();
                var published = writer.writePrepared(connection, prepared, checkpoint, JavaWriterCandidate.InterruptPoint.NONE);
                preserved.set(writer.phaseSeconds().getOrDefault("preserved_output_rows", 0.0).longValue());
                return published;
            }
        });
        double persistenceSeconds = (System.nanoTime() - started) / 1e9;
        JsonObject item = JsonSupport.object();
        item.put("session", result.session().toString());
        item.put("family", family);
        item.put("window", result.window());
        item.put("row_count", result.rowCount());
        item.put("ready_count", result.readyCount());
        item.put("content_digest", result.contentDigest());
        item.put("preserved_output_rows", preserved.get());
        item.put("seconds", preparationSeconds + persistenceSeconds);
        JsonObject phases = JsonSupport.object();
        prepared.phases().forEach(phases::put);
        phases.put("persistence", persistenceSeconds);
        item.put("phase_seconds", phases);
        synchronized (progress) {
            progress.add(item);
            Path temporary = checkpoints.resolve("pipeline-progress.tmp");
            Files.write(temporary, CanonicalJson.bytes(progress));
            Files.move(temporary, checkpoints.resolve("pipeline-progress.json"), StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING);
        }
        return item;
    }

    private Connection connect() throws java.sql.SQLException {
        return new MySqlConnectionFactory().open(configuration);
    }

    private JsonObject tune(List<LocalDate> sessions) throws Exception {
        if (sessions.size() < 10) throw new IllegalArgumentException("Tuning up to ten workers requires at least ten target sessions");
        List<LocalDate> dates = sessions.stream().flatMap(session -> manifest.target(session).zscoreDependencies().stream()).distinct().sorted().toList();
        source.request(dates).join();
        prepareOnly(sessions, 3);
        Map<Integer, List<Double>> samples = new java.util.TreeMap<>();
        List<String> reference = null;
        JsonArray trials = JsonSupport.array();
        for (int repetition = 0; repetition < 2; repetition++) {
            for (int offset = 1; offset <= 10; offset++) {
                int count = repetition == 0 ? offset : 11 - offset;
                long started = System.nanoTime();
                List<String> digests = prepareOnly(sessions, count);
                double seconds = (System.nanoTime() - started) / 1e9;
                if (reference == null) reference = digests;
                else if (!reference.equals(digests)) throw new IllegalStateException("Worker tuning changed canonical results");
                samples.computeIfAbsent(count, ignored -> new ArrayList<>()).add(seconds);
                JsonObject trial = JsonSupport.addObject(trials);
                trial.put("workers", count);
                trial.put("repetition", repetition);
                trial.put("seconds", seconds);
                Path temporary = checkpoints.resolve("tuning-progress.tmp");
                Files.write(temporary, CanonicalJson.bytes(trial));
                Files.move(temporary, checkpoints.resolve("tuning-progress.json"), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            }
        }
        Map<Integer, Double> medians = new java.util.TreeMap<>();
        samples.forEach((count, values) -> medians.put(count, (values.get(0) + values.get(1)) / 2));
        int selected = selectWorkers(medians);
        resize(selected);
        JsonObject report = JsonSupport.object();
        report.put("status", "complete");
        report.put("selected_workers", selected);
        report.put("trials", trials);
        report.put("scope", "cached real-input calculation and hashing only; no output writes; loader/coordinator/writer excluded from calculation count");
        report.put("selection_rule", "smallest worker count within 5 percent of best median; two warm trials per count, reversed second sweep");
        report.put("source_batches", source.batchCount());
        JsonArray selectedSessions = JsonSupport.putArray(report, "sessions");
        sessions.forEach(session -> selectedSessions.add(session.toString()));
        return report;
    }

    static int selectWorkers(Map<Integer, Double> medians) {
        double best = medians.values().stream().mapToDouble(Double::doubleValue).min().orElseThrow();
        return medians.entrySet().stream().filter(entry -> entry.getValue() <= best * 1.05)
                .mapToInt(Map.Entry::getKey).min().orElseThrow();
    }

    private void resize(int count) {
        if (count > calculators.getMaximumPoolSize()) calculators.setMaximumPoolSize(count);
        calculators.setCorePoolSize(count);
        calculators.setMaximumPoolSize(count);
        workers = count;
    }

    private List<String> prepareOnly(List<LocalDate> sessions, int count) throws Exception {
        resize(count);
        List<Future<List<String>>> pending = new ArrayList<>();
        for (LocalDate session : sessions) {
            pending.add(calculators.submit(() -> {
                List<String> result = new ArrayList<>();
                for (JavaZScoreCandidate.Family family : JavaZScoreCandidate.Family.values()) {
                    for (int window : new int[]{20, 60}) {
                        var prepared = candidate().prepare(source, session, family, window);
                        result.add(session + "/" + family + "/" + window + "/" + prepared.snapshot().contentDigest());
                    }
                }
                return result;
            }));
        }
        List<String> results = new ArrayList<>();
        for (var future : pending) results.addAll(future.get());
        return results.stream().sorted().toList();
    }

    static <Value> Value retryPublication(SqlWork<Value> work) throws Exception {
        for (int attempt = 0; ; attempt++) {
            try {
                return work.run();
            } catch (java.sql.SQLException error) {
                if (attempt >= 2 || (error.getErrorCode() != 1213 && error.getErrorCode() != 1205)) throw error;
                Thread.sleep(50L * (attempt + 1));
            }
        }
    }

    @FunctionalInterface
    interface SqlWork<Value> {
        Value run() throws Exception;
    }

    private JavaZScoreCandidate candidate() {
        return new JavaZScoreCandidate(manifest, production, configuration.database());
    }

    @Override
    public void close() throws Exception {
        calculators.shutdownNow();
        publishers.shutdownNow();
        boolean calculationsStopped = calculators.awaitTermination(30, TimeUnit.SECONDS);
        boolean publicationsStopped = publishers.awaitTermination(30, TimeUnit.SECONDS);
        source.close();
        if (!calculationsStopped || !publicationsStopped) throw new IllegalStateException("Pipeline workers did not stop");
    }

    private static DatabaseConfiguration targetConfiguration(Map<String, String> arguments) {
        DatabaseConfiguration configured = DatabaseConfiguration.load(Path.of(required(arguments, "dbinfo-file")));
        if (Boolean.parseBoolean(arguments.getOrDefault("production", "false"))) return configured;
        String schema = required(arguments, "disposable-schema");
        if (!schema.matches("stockguidance_disposable_[a-z0-9_]{1,39}") || schema.equalsIgnoreCase(configured.database())) {
            throw new IllegalArgumentException("Require distinct disposable schema");
        }
        return new DatabaseConfiguration(configured.host(), schema, configured.user(), configured.password(),
                configured.port(), configured.driver(), configured.tableNamesFile());
    }

    private static int workerCount(Map<String, String> arguments, String name, int fallback) {
        int count = Integer.parseInt(arguments.getOrDefault(name, Integer.toString(fallback)));
        if (count < 1 || count > 128) throw new IllegalArgumentException("Worker count must be between 1 and 128");
        return count;
    }

    private static List<LocalDate> parseSessions(String value) {
        List<LocalDate> result = new ArrayList<>();
        for (String item : value.split(",")) result.add(LocalDate.parse(item));
        if (result.isEmpty() || new HashSet<>(result).size() != result.size() || result.size() > 20) {
            throw new IllegalArgumentException("Require one to twenty distinct target sessions");
        }
        return List.copyOf(result);
    }

    private static String required(Map<String, String> arguments, String name) {
        String value = arguments.get(name);
        if (value == null || value.isBlank()) throw new IllegalArgumentException("Missing " + name);
        return value;
    }

    private static Map<String, String> parse(String[] arguments) {
        if (arguments.length % 2 != 0) throw new IllegalArgumentException("Require named pairs");
        Map<String, String> result = new HashMap<>();
        for (int index = 0; index < arguments.length; index += 2) {
            if (!arguments[index].startsWith("--") || result.putIfAbsent(arguments[index].substring(2), arguments[index + 1]) != null) {
                throw new IllegalArgumentException("Invalid or duplicate argument");
            }
        }
        return result;
    }

    private static void print(JsonObject value) {
        System.out.println(new String(CanonicalJson.bytes(value), StandardCharsets.UTF_8).strip());
        System.out.flush();
    }
}
