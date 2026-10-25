package stockguidance.migration;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import stockguidance.json.JsonSupport;

import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.*;

class RowQualityTest {
    @TempDir Path directory;
    private static final Instant NOW = Instant.parse("2024-07-10T23:00:00Z");

    private RowQuality.Observed observed(int sequence, int price) {
        BigDecimal value = BigDecimal.valueOf(price);
        return new RowQuality.Observed(sequence + 1, 1, "AAA", LocalDate.of(2024, 7, 1).plusDays(sequence), sequence,
                Arrays.asList(value, value, value, value, BigDecimal.ZERO, null, BigDecimal.ZERO), NOW.minusSeconds(86400));
    }

    @Test void classifiesSingletonAndConsecutiveGapsAndPreservesOriginals() {
        for (int gap : List.of(1, 2, 5)) {
            List<RowQuality.Observed> inputs = new ArrayList<>(); inputs.add(observed(0, 10));
            for (int index = 1; index <= gap; index++) inputs.add(observed(index, 0));
            inputs.add(observed(gap + 1, 10 + gap + 1));
            var repairs = RowQuality.interpolate(inputs, NOW);
            assertEquals(gap, repairs.size());
            for (int index = 0; index < gap; index++) {
                var repair = repairs.get(index);
                assertEquals(0, repair.observed().values().getFirst().signum());
                assertEquals(new BigDecimal(11 + index).setScale(8), repair.effective().getFirst());
                assertEquals(RowQuality.BAD | (gap == 1 ? RowQuality.SINGLE : RowQuality.GAP), repair.flags());
                assertEquals(gap, repair.gapLength());
            }
        }
    }

    @Test void unresolvedEndpointsRemainBadAndNullableFieldsStayNull() {
        var repair = RowQuality.interpolate(List.of(observed(0, 0), observed(1, 10)), NOW).getFirst();
        assertEquals(RowQuality.BAD, repair.flags()); assertNull(repair.effective());
        assertTrue(RowQuality.interpolate(List.of(observed(0, 10)), NOW).isEmpty());
    }

    @Test void policyControlsAvailabilityAndPropagatesFutureLeakage() {
        var payload = RowQuality.interpolate(List.of(observed(0, 10), observed(1, 0), observed(2, 12)), NOW).getFirst().payload();
        var policy = JsonSupport.object(); policy.put("quality_history", "interpolated");
        policy.put("quality_cutoff", NOW.minusSeconds(3600).toString());
        assertFalse(RowQuality.cell(0.0, payload, policy).accepted());
        policy.put("retrospective_quality", true);
        var cell = RowQuality.cell(0.0, payload, policy);
        assertTrue(cell.accepted()); assertEquals(11.0, cell.value());
        assertNotEquals(0, cell.quality() & RowQuality.FUTURE_LEAKAGE);
        policy.put("quality_history", "clean"); assertFalse(RowQuality.cell(0.0, payload, policy).accepted());
    }

    @Test void importReaderRetainsBadRowsAndFractionalCountsWithoutChangingSource() throws Exception {
        LocalDate session = LocalDate.of(2024, 7, 1);
        long timestamp = session.atStartOfDay(java.time.ZoneId.of("America/New_York")).toInstant().toEpochMilli();
        String source = "{\"T\":\"TTE\",\"o\":0,\"h\":0,\"l\":0,\"c\":0,\"v\":0,\"vw\":null,\"n\":0,\"t\":" + timestamp + "}\n"
                + "{\"T\":\"AAA\",\"o\":10,\"h\":10,\"l\":10,\"c\":10,\"v\":100,\"vw\":null,\"n\":1.5,\"t\":" + timestamp + "}\n";
        Path path = directory.resolve("2024-07-01.ndjson"); Files.writeString(path, source);
        assertThrows(IllegalArgumentException.class, () -> ArchiveCandidateReader.read(path, session));
        var archive = ArchiveCandidateReader.readForImport(path, session);
        assertEquals(2, archive.bars().size()); assertEquals(2, archive.badRecords().size());
        assertEquals(new BigDecimal("1.5"), archive.badRecords().get("AAA").get(6));
        assertEquals(ArchiveCandidateReader.recordsId(java.util.Map.of()), archive.recordsId());
        assertEquals(source, Files.readString(path));
    }
}
