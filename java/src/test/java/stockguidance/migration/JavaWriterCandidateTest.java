package stockguidance.migration;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import stockguidance.validation.LegacyDerivedCalculator;
import stockguidance.validation.ValidationContent;

import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.LocalDate;
import java.util.HexFormat;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class JavaWriterCandidateTest {
    @TempDir
    Path directory;

    @Test
    void exactReaderPreservesFractionalVolumeExtremePriceAndCaseDistinctTickers() throws Exception {
        Path file = directory.resolve("2024-07-01.ndjson");
        Files.writeString(file,
                "{\"T\":\"AAP\",\"o\":1702500000000000.0,\"h\":1702500000000000.1,\"l\":1702500000000000.0,\"c\":1702500000000000.1,\"v\":61812.9999,\"vw\":null,\"n\":10,\"t\":1719864000000}\n"
                        + "{\"T\":\"AAp\",\"o\":10,\"h\":12,\"l\":9,\"c\":11,\"v\":14982.4,\"vw\":10.5,\"n\":null,\"t\":1719864000000}\n");

        ArchiveCandidateReader.SessionArchive archive = ArchiveCandidateReader.read(file, LocalDate.of(2024, 7, 1));

        assertEquals(2, archive.bars().size());
        assertEquals(new BigDecimal("61812.9999"), archive.bars().get("AAP").volume());
        assertEquals(0, new BigDecimal("1702500000000000.0").compareTo(archive.bars().get("AAP").open()));
        assertEquals(0, new BigDecimal("1702500000000000.1").compareTo(archive.bars().get("AAP").high()));
        assertEquals(64, archive.recordsId().length());
        assertNotEquals(archive.bars().get("AAP"), archive.bars().get("AAp"));
    }

    @Test
    void readerRejectsDuplicateJsonFields() throws Exception {
        Path file = directory.resolve("2024-07-01.ndjson");
        Files.writeString(file,
                "{\"T\":\"AAP\",\"o\":10,\"o\":11,\"h\":12,\"l\":9,\"c\":11,\"v\":100,\"t\":1719864000000}\n");

        assertThrows(IllegalArgumentException.class, () -> ArchiveCandidateReader.read(
                file, LocalDate.of(2024, 7, 1)));
    }

    @Test
    void readerRejectsChangedContentInsteadOfAdoptingAStaleIdentity() throws Exception {
        Path file = directory.resolve("2024-07-01.ndjson");
        Files.writeString(file, "{\"T\":\"AAP\",\"o\":10,\"h\":12,\"l\":9,\"c\":11,\"v\":100.4,\"t\":1719864000000}\n");
        ArchiveCandidateReader.SessionArchive original = ArchiveCandidateReader.read(file, LocalDate.of(2024, 7, 1));
        Files.writeString(file, "{\"T\":\"AAP\",\"o\":10,\"h\":12,\"l\":9,\"c\":11,\"v\":100.5,\"t\":1719864000000}\n");
        ArchiveCandidateReader.SessionArchive changed = ArchiveCandidateReader.read(file, LocalDate.of(2024, 7, 1));

        assertNotEquals(original.fileSha256(), changed.fileSha256());
        assertNotEquals(original.recordsId(), changed.recordsId());
    }

    @Test
    void blockReaderPreservesHashesAcrossBoundariesAndLineEndings() throws Exception {
        String record = "{\"T\":\"AAP\",\"o\":10,\"h\":12,\"l\":9,\"c\":11,\"v\":100.4,\"t\":1719864000000}";
        String second = record.replace("AAP", "AAp");
        String recordsId = null;
        for (int boundary : new int[]{65535, 65536, 65537, 131073}) {
            for (String ending : new String[]{"\n", "\r\n"}) {
                for (boolean finalNewline : new boolean[]{false, true}) {
                    String content = " ".repeat(boundary - record.length()) + record + ending
                            + second + (finalNewline ? ending : "");
                    Path file = directory.resolve("boundary.ndjson");
                    byte[] bytes = content.getBytes(StandardCharsets.UTF_8);
                    Files.write(file, bytes);
                    ArchiveCandidateReader.SessionArchive archive = ArchiveCandidateReader.read(file, LocalDate.of(2024, 7, 1));
                    assertEquals(2, archive.bars().size());
                    assertEquals(HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes)), archive.fileSha256());
                    if (recordsId == null) recordsId = archive.recordsId();
                    assertEquals(recordsId, archive.recordsId());
                }
            }
        }
    }

    @Test
    void blockReaderRejectsEmptyLinesAndDuplicateTickers() throws Exception {
        String record = "{\"T\":\"AAP\",\"o\":10,\"h\":12,\"l\":9,\"c\":11,\"v\":100,\"t\":1719864000000}";
        Path file = directory.resolve("invalid.ndjson");
        for (String content : new String[]{"", "\n", "\r\n", record + "\n\n", record + "\n" + record}) {
            Files.writeString(file, content);
            assertThrows(IllegalArgumentException.class, () -> ArchiveCandidateReader.read(file, LocalDate.of(2024, 7, 1)));
        }
    }

    @Test
    void derivedOracleKeepsExactCaseAndNullableInputs() {
        ValidationContent.RawBar priorA = bar("AAP", "100.40000000", "10.00000000", null, 10L);
        ValidationContent.RawBar priora = bar("AAp", "200.00000000", "10.00000000", null, null);
        ValidationContent.RawBar currentA = bar("AAP", "101.40000000", "11.00000000", null, 10L);
        ValidationContent.RawBar currenta = bar("AAp", "200.40000000", "11.00000000", "11.1", null);
        ValidationContent.RawBar followingA = bar("AAP", "102.40000000", "11.50000000", null, 10L);
        ValidationContent.RawBar followinga = bar("AAp", "201.40000000", "11.50000000", "11.6", null);

        Map<String, java.util.List<Double>> rows = LegacyDerivedCalculator.expected(
                Map.of("AAP", priorA, "AAp", priora),
                Map.of("AAP", currentA, "AAp", currenta),
                Map.of("AAP", followingA, "AAp", followinga));

        assertEquals(2, rows.size());
        assertEquals(16, rows.get("AAP").size());
        assertEquals(101.4 / 100.4, rows.get("AAP").get(4), 1e-12);
        assertEquals(null, rows.get("AAp").get(5));
    }

    @Test
    void derivedMetadataUsesPythonFloatEncoding() {
        assertEquals("1.0", JavaWriterCandidate.pythonFloat(1.0));
        assertEquals("-0.0", JavaWriterCandidate.pythonFloat(-0.0));
        assertEquals("0.0001", JavaWriterCandidate.pythonFloat(0.0001));
        assertEquals("1e-05", JavaWriterCandidate.pythonFloat(0.00001));
        assertEquals("10000000.0", JavaWriterCandidate.pythonFloat(1e7));
        assertEquals("1e+16", JavaWriterCandidate.pythonFloat(1e16));
    }

    @Test
    void interruptionPointsAreExplicitAndUnknownCheckpointIsRejected() {
        assertThrows(JavaWriterCandidate.InterruptionException.class,
                () -> JavaWriterCandidate.applyInterruption(JavaWriterCandidate.InterruptPoint.BEFORE_COMMIT, JavaWriterCandidate.InterruptPoint.BEFORE_COMMIT));
        assertThrows(JavaWriterCandidate.InterruptionException.class,
                () -> JavaWriterCandidate.applyInterruption(JavaWriterCandidate.InterruptPoint.AFTER_COMMIT, JavaWriterCandidate.InterruptPoint.AFTER_COMMIT));
    }

    private static ValidationContent.RawBar bar(String ticker, String volume, String close, String vwap, Long transactions) {
        return new ValidationContent.RawBar(ticker, new BigDecimal("10.00000000"), new BigDecimal("12.00000000"),
                new BigDecimal("9.00000000"), new BigDecimal(close), new BigDecimal(volume),
                vwap == null ? null : new BigDecimal(vwap), transactions);
    }
}
