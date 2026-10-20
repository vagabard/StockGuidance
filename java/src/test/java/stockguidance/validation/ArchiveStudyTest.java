package stockguidance.validation;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
import stockguidance.json.JsonSupport;

import java.nio.file.Files;
import java.nio.file.Path;
import java.math.BigDecimal;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class ArchiveStudyTest {
    @TempDir
    Path directory;

    @Test
    void serialAndParallelProduceTheSameDigest() throws Exception {
        Path file = directory.resolve("2024-07-01.ndjson");
        Files.writeString(file, "{\"T\":\"AAA\",\"o\":10,\"h\":12,\"l\":9,\"c\":11,\"v\":100,\"t\":1719864000000}\n");
        var request = JsonSupport.object();
        JsonArray paths = JsonSupport.putArray(request, "paths");
        paths.add(file.toString());
        request.put("mode", "files"); request.put("workers", 1); request.put("repeats", 2);
        var serial = ArchiveStudyMain.run(request);
        request.put("workers", 3);
        var parallel = ArchiveStudyMain.run(request);
        JsonArray serialMeasurements = (JsonArray) serial.get("measurements");
        JsonArray parallelMeasurements = (JsonArray) parallel.get("measurements");
        assertEquals(((JsonObject) serialMeasurements.get(0)).get("digest"), ((JsonObject) parallelMeasurements.get(1)).get("digest"));
    }

    @Test
    void rejectsExtraWorkersAndOverflowedCounts() throws Exception {
        assertThrows(IllegalArgumentException.class, () -> ArchiveStudyMain.run(JsonSupport.parse("{\"workers\":4,\"mode\":\"files\"}")));
        Path file = directory.resolve("2024-07-01.ndjson");
        Files.writeString(file, "{\"T\":\"AAA\",\"o\":10,\"h\":12,\"l\":9,\"c\":11,\"v\":100,\"t\":1719864000000,\"n\":9223372036854775808}\n");
        var request = JsonSupport.object();
        JsonSupport.putArray(request, "paths").add(file.toString());
        request.put("mode", "files"); request.put("workers", 1); request.put("repeats", 1);
        assertThrows(java.util.concurrent.ExecutionException.class, () -> ArchiveStudyMain.run(request));
    }

    @Test
    void sharedDerivedCalculatorProducesAllLegacyColumns() {
        var prior = Map.of("AAA", bar("10.0000"));
        var current = Map.of("AAA", bar("11.0000"));
        var following = Map.of("AAA", bar("12.0000"));
        var expected = LegacyDerivedCalculator.expected(prior, current, following);

        assertEquals(16, expected.get("AAA").size());
        assertEquals(1.0, expected.get("AAA").get(0));
        assertEquals(1.1, expected.get("AAA").get(7));
        assertEquals(1.2, expected.get("AAA").get(8));
        assertEquals(0, LegacyDerivedCalculator.differingColumns(expected.get("AAA"), expected.get("AAA")).size());
        assertEquals(64, LegacyDerivedCalculator.studyId(expected).length());
    }

    private static ValidationContent.RawBar bar(String close) {
        return new ValidationContent.RawBar("AAA", new BigDecimal("10.0000"), new BigDecimal("12.0000"), new BigDecimal("9.0000"), new BigDecimal(close), 100, null, null);
    }
}
