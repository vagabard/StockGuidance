package stockguidance.migration;

import org.apache.fory.json.JsonObject;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import stockguidance.json.CanonicalJson;
import stockguidance.json.JsonSupport;

import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDate;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class JavaDailyMaintenanceManifestTest {
    @TempDir
    Path directory;

    @Test
    void operationalWorkloadDoesNotRequireMigrationArtifactsButVerifiesContent() throws Exception {
        Path archivePath = directory.resolve("2024-07-01.ndjson");
        Files.writeString(archivePath, "{\"T\":\"ABC\",\"o\":\"10.12345678\",\"h\":12,\"l\":9,\"c\":11,\"v\":\"123.00000001\",\"vw\":\"10.1234567890123456789012\",\"t\":1719864000000}\n");
        LocalDate session = LocalDate.of(2024, 7, 1);
        var archive = ArchiveCandidateReader.read(archivePath, session);
        JsonObject root = JsonSupport.parse("{\"schema_version\":1,\"kind\":\"daily-maintenance\",\"source_key\":\"configured-source\",\"authoritative_registry\":{\"schema_version\":1,\"tables\":{\"source\":\"stockguidance_source\",\"sessions\":\"transaction_dates\",\"tickers\":\"ticker_symbol\",\"prices\":\"raw_stock_data\",\"deltas\":\"daily_delta_stock_data\",\"imports\":\"stockguidance_imports\",\"zscore_series\":\"stockguidance_zscore_series\",\"zscore_sessions\":\"stockguidance_zscore_sessions\"},\"zscore_templates\":{\"price\":\"zscores_price_{window}\",\"price_delta\":\"zscores_price_delta_{window}\"}},\"targets\":[{\"session\":\"2024-07-01\",\"derived_dependencies\":[\"2024-06-28\",\"2024-07-02\"],\"zscore_dependencies\":[]}],\"files\":[]}");
        root.put("data_root", directory.toString());
        JsonObject file = JsonSupport.addObject((org.apache.fory.json.JsonArray) root.get("files"));
        file.put("session", session.toString());
        file.put("path", archivePath.toString());
        file.put("valid", true);
        file.put("file_sha256", archive.fileSha256());
        file.put("records_id", archive.recordsId());
        root.put("manifest_id", CanonicalJson.sha256(root));
        Path workload = directory.resolve("workload.json");
        Files.write(workload, CanonicalJson.bytes(root));
        var manifest = JavaWriterManifest.read(workload);
        assertTrue(manifest.operational());
        assertEquals(archive.recordsId(), manifest.archive(session).recordsId());
        Files.writeString(archivePath, "changed input");
        assertThrows(IllegalArgumentException.class, () -> manifest.archive(session));
        root.put("source_key", "changed-source");
        Files.write(workload, CanonicalJson.bytes(root));
        assertThrows(IllegalArgumentException.class, () -> JavaWriterManifest.read(workload));
    }

    @Test
    void operationalWorkloadTrustsExistingFileWithHistoricalRecordIdentity() throws Exception {
        Path archivePath = directory.resolve("2024-07-01.ndjson");
        Files.writeString(archivePath, "{\"T\":\"ABC\",\"o\":\"10.12345678\",\"h\":12,\"l\":9,\"c\":11,\"v\":\"123.00000001\",\"vw\":\"10.1234567890123456789012\",\"t\":1719864000000}\n");
        LocalDate session = LocalDate.of(2024, 7, 1);
        var archive = ArchiveCandidateReader.read(archivePath, session);
        JsonObject root = JsonSupport.parse("{\"schema_version\":1,\"kind\":\"daily-maintenance\",\"source_key\":\"configured-source\",\"authoritative_registry\":{\"schema_version\":1,\"tables\":{\"source\":\"stockguidance_source\",\"sessions\":\"transaction_dates\",\"tickers\":\"ticker_symbol\",\"prices\":\"raw_stock_data\",\"deltas\":\"daily_delta_stock_data\",\"imports\":\"stockguidance_imports\",\"zscore_series\":\"stockguidance_zscore_series\",\"zscore_sessions\":\"stockguidance_zscore_sessions\"},\"zscore_templates\":{\"price\":\"zscores_price_{window}\",\"price_delta\":\"zscores_price_delta_{window}\"}},\"targets\":[{\"session\":\"2024-07-01\",\"derived_dependencies\":[\"2024-06-28\",\"2024-07-02\"],\"zscore_dependencies\":[]}],\"files\":[]}");
        root.put("data_root", directory.toString());
        JsonObject file = JsonSupport.addObject((org.apache.fory.json.JsonArray) root.get("files"));
        file.put("session", session.toString());
        file.put("path", archivePath.toString());
        file.put("valid", true);
        file.put("file_sha256", null);
        file.put("records_id", "historical-database-identity");
        root.put("manifest_id", CanonicalJson.sha256(root));
        Path workload = directory.resolve("trusted-workload.json");
        Files.write(workload, CanonicalJson.bytes(root));

        var manifest = JavaWriterManifest.read(workload);
        assertTrue(manifest.trustsExistingFile(session));
        assertEquals(archive.recordsId(), manifest.archive(session).recordsId());
    }

    @Test
    void migrationManifestsKeepTheirStageAndEvidenceRequirements() throws Exception {
        Path workload = directory.resolve("invalid-migration.json");
        Files.writeString(workload, "{\"schema_version\":1,\"stage\":15}");
        assertThrows(IllegalArgumentException.class, () -> JavaWriterManifest.read(workload));
    }
}
