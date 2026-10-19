package stockguidance.validation;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import stockguidance.json.JsonSupport;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class ArchiveStudyTest {
    @TempDir
    Path directory;

    @Test
    void serialAndParallelProduceTheSameDigest() throws Exception {
        Path file = directory.resolve("2024-07-01.ndjson");
        Files.writeString(file, "{\"T\":\"AAA\",\"o\":10,\"h\":12,\"l\":9,\"c\":11,\"v\":100,\"t\":1719864000000}\n");
        var request = com.fasterxml.jackson.databind.node.JsonNodeFactory.instance.objectNode();
        request.putArray("paths").add(file.toString());
        request.put("mode", "files").put("workers", 1).put("repeats", 2);
        var serial = ArchiveStudyMain.run(request);
        request.put("workers", 3);
        var parallel = ArchiveStudyMain.run(request);
        assertEquals(serial.path("measurements").get(0).path("digest"), parallel.path("measurements").get(1).path("digest"));
    }

    @Test
    void rejectsExtraWorkersAndOverflowedCounts() throws Exception {
        assertThrows(IllegalArgumentException.class, () -> ArchiveStudyMain.run(JsonSupport.parse("{\"workers\":4,\"mode\":\"files\"}")));
        Path file = directory.resolve("2024-07-01.ndjson");
        Files.writeString(file, "{\"T\":\"AAA\",\"o\":10,\"h\":12,\"l\":9,\"c\":11,\"v\":100,\"t\":1719864000000,\"n\":9223372036854775808}\n");
        var request = com.fasterxml.jackson.databind.node.JsonNodeFactory.instance.objectNode();
        request.putArray("paths").add(file.toString());
        request.put("mode", "files").put("workers", 1).put("repeats", 1);
        assertThrows(java.util.concurrent.ExecutionException.class, () -> ArchiveStudyMain.run(request));
    }
}
