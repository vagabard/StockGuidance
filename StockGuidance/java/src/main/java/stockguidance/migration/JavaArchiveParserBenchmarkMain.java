package stockguidance.migration;

import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import stockguidance.json.CanonicalJson;

import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.EnumMap;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public final class JavaArchiveParserBenchmarkMain {
    private JavaArchiveParserBenchmarkMain() {
    }

    public static void main(String[] arguments) {
        try {
            System.out.print(new String(CanonicalJson.bytes(run(parse(arguments))), StandardCharsets.UTF_8));
        } catch (Exception error) {
            System.out.print("{\"failed\":true,\"error\":\"Java archive parser benchmark failed\",\"failure_type\":\""
                    + error.getClass().getSimpleName() + "\"}\n");
            System.exit(1);
        }
    }

    static ObjectNode run(Map<String, String> arguments) {
        JavaWriterManifest manifest = JavaWriterManifest.read(Path.of(required(arguments, "manifest")));
        List<LocalDate> sessions = parseSessions(required(arguments, "sessions"));
        int warmups = Integer.parseInt(arguments.getOrDefault("warmups", "1"));
        int repetitions = Integer.parseInt(arguments.getOrDefault("repetitions", "4"));
        if (warmups < 1 || repetitions < 1) {
            throw new IllegalArgumentException("Warmups and repetitions must be positive");
        }

        Map<ArchiveCandidateReader.Parser, ParseSummary> reference = new EnumMap<>(ArchiveCandidateReader.Parser.class);
        for (ArchiveCandidateReader.Parser parser : ArchiveCandidateReader.Parser.values()) {
            reference.put(parser, parseAll(manifest, sessions, parser));
        }
        for (int repetition = 0; repetition < warmups; repetition++) {
            runTimedParse(manifest, sessions, repetition % 2 == 0
                    ? ArchiveCandidateReader.Parser.JACKSON : ArchiveCandidateReader.Parser.FORY, reference);
            runTimedParse(manifest, sessions, repetition % 2 == 0
                    ? ArchiveCandidateReader.Parser.FORY : ArchiveCandidateReader.Parser.JACKSON, reference);
        }

        Map<ArchiveCandidateReader.Parser, List<Double>> timings = new EnumMap<>(ArchiveCandidateReader.Parser.class);
        for (ArchiveCandidateReader.Parser parser : ArchiveCandidateReader.Parser.values()) {
            timings.put(parser, new ArrayList<>());
        }
        for (int repetition = 0; repetition < repetitions; repetition++) {
            ArchiveCandidateReader.Parser first = repetition % 2 == 0
                    ? ArchiveCandidateReader.Parser.JACKSON : ArchiveCandidateReader.Parser.FORY;
            ArchiveCandidateReader.Parser second = first == ArchiveCandidateReader.Parser.JACKSON
                    ? ArchiveCandidateReader.Parser.FORY : ArchiveCandidateReader.Parser.JACKSON;
            timings.get(first).add(runTimedParse(manifest, sessions, first, reference));
            timings.get(second).add(runTimedParse(manifest, sessions, second, reference));
        }

        ObjectNode report = JsonNodeFactory.instance.objectNode();
        report.put("schema_version", 1);
        report.put("status", "complete");
        report.put("benchmark", "stockguidance-java-archive-parser-ab-v1");
        report.put("timing_scope", "archive file read, SHA-256, JSON decode, validation, and records identity; JVM startup excluded");
        report.put("sessions", sessions.size());
        report.put("rows", reference.get(ArchiveCandidateReader.Parser.JACKSON).rows());
        report.put("warmups", warmups);
        report.put("repetitions", repetitions);
        ObjectNode results = report.putObject("results");
        for (ArchiveCandidateReader.Parser parser : ArchiveCandidateReader.Parser.values()) {
            List<Double> values = timings.get(parser);
            values.sort(Comparator.naturalOrder());
            ObjectNode result = results.putObject(parser.name().toLowerCase(java.util.Locale.ROOT));
            result.put("parity_passed", true);
            result.put("median_seconds", values.get(values.size() / 2));
            result.put("mean_seconds", values.stream().mapToDouble(Double::doubleValue).average().orElseThrow());
            result.put("rows_per_second", reference.get(parser).rows() / values.stream().mapToDouble(Double::doubleValue).average().orElseThrow());
            ArrayNode samples = result.putArray("seconds");
            values.forEach(samples::add);
        }
        return report;
    }

    private static double runTimedParse(JavaWriterManifest manifest, List<LocalDate> sessions,
                                        ArchiveCandidateReader.Parser parser,
                                        Map<ArchiveCandidateReader.Parser, ParseSummary> reference) {
        long started = System.nanoTime();
        ParseSummary actual = parseAll(manifest, sessions, parser);
        ParseSummary expected = reference.get(parser);
        ParseSummary other = reference.get(parser == ArchiveCandidateReader.Parser.JACKSON
                ? ArchiveCandidateReader.Parser.FORY : ArchiveCandidateReader.Parser.JACKSON);
        if (!actual.equals(expected) || actual.rows() != other.rows() || !actual.identities().equals(other.identities())) {
            throw new IllegalStateException("Parser result changed during benchmark: " + parser);
        }
        return (System.nanoTime() - started) / 1_000_000_000.0;
    }

    private static ParseSummary parseAll(JavaWriterManifest manifest, List<LocalDate> sessions,
                                         ArchiveCandidateReader.Parser parser) {
        long rows = 0;
        Map<LocalDate, String> identities = new HashMap<>();
        for (LocalDate session : sessions) {
            ArchiveCandidateReader.SessionArchive archive = manifest.archive(session, parser);
            rows += archive.bars().size();
            identities.put(session, archive.recordsId());
        }
        return new ParseSummary(rows, identities);
    }

    private static List<LocalDate> parseSessions(String value) {
        List<LocalDate> sessions = Arrays.stream(value.split(","))
                .map(String::trim)
                .filter(item -> !item.isEmpty())
                .map(LocalDate::parse)
                .toList();
        if (sessions.isEmpty()) throw new IllegalArgumentException("At least one session is required");
        return sessions;
    }

    private static Map<String, String> parse(String[] arguments) {
        Map<String, String> result = new HashMap<>();
        for (int index = 0; index < arguments.length; index++) {
            String argument = arguments[index];
            if (!argument.startsWith("--") || index + 1 >= arguments.length || arguments[index + 1].startsWith("--")) {
                throw new IllegalArgumentException("Arguments require --name value pairs");
            }
            if (result.putIfAbsent(argument.substring(2), arguments[++index]) != null) {
                throw new IllegalArgumentException("Duplicate argument: " + argument);
            }
        }
        return result;
    }

    private static String required(Map<String, String> arguments, String name) {
        String value = arguments.get(name);
        if (value == null || value.isBlank()) throw new IllegalArgumentException("Missing argument: --" + name);
        return value;
    }

    private record ParseSummary(long rows, Map<LocalDate, String> identities) {
    }
}
