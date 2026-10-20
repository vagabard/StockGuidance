package stockguidance.migration;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import stockguidance.validation.LegacyDerivedCalculator;
import stockguidance.validation.ValidationContent;

import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDate;
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
        assertEquals(64, archive.recordsId().length());
        assertNotEquals(archive.bars().get("AAP"), archive.bars().get("AAp"));
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
