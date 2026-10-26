package stockguidance.database;

import org.junit.jupiter.api.Test;
import stockguidance.validation.ValidationContent;

import java.math.BigDecimal;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

class SimplifiedNumericalKernelTest {
    private static ValidationContent.RawBar bar(String ticker, String close) {
        BigDecimal value = new BigDecimal(close);
        return new ValidationContent.RawBar(ticker, new BigDecimal("10.00000000"), value.add(BigDecimal.ONE),
                new BigDecimal("9.00000000"), value, new BigDecimal("100.00000000"), value, 5L);
    }

    @Test
    void reusesLegacyFormulasAndMarksLatestFutureLabelsExplicitly() {
        var prior = new SimplifiedNumericalKernel.RawObservation(1, "AAA", bar("AAA", "10"), SimplifiedContracts.DataState.GOOD, true, 0, 0, List.of(), null);
        var current = new SimplifiedNumericalKernel.RawObservation(1, "AAA", bar("AAA", "12"), SimplifiedContracts.DataState.GOOD, true, 0, 0, List.of(), null);
        var following = new SimplifiedNumericalKernel.RawObservation(1, "AAA", bar("AAA", "15"), SimplifiedContracts.DataState.GOOD, true, 0, 0, List.of(), null);
        var ready = SimplifiedNumericalKernel.calculateDeltas(List.of(prior), List.of(current), List.of(following), true).getFirst();

        assertEquals(SimplifiedContracts.CalculationStatus.READY, ready.status());
        assertEquals(1.2, ready.values().get(3), 1e-12);
        assertEquals(15.0 / 10.0, ready.values().get(8), 1e-12);

        var latest = SimplifiedNumericalKernel.calculateDeltas(List.of(prior), List.of(current), List.of(), false).getFirst();
        assertEquals(SimplifiedContracts.CalculationStatus.NOT_YET_AVAILABLE, latest.status());
        assertEquals(1.2, latest.values().get(3), 1e-12);
        assertNull(latest.values().get(8));
    }
}
