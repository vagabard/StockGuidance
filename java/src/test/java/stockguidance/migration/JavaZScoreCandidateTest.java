package stockguidance.migration;

import org.junit.jupiter.api.Test;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;

class JavaZScoreCandidateTest {
    @Test
    void priceAndDeltaFamiliesUseDenseCaseSensitiveTickerScope() {
        List<LocalDate> dates = dates(4);
        Map<Long, String> tickers = Map.of(11L, "AAP", 13L, "AAp");
        Map<Long, Map<LocalDate, JavaZScoreCandidate.Cell>> raw = new LinkedHashMap<>();
        raw.put(11L, values(dates, 10, 11, 12, 13));
        raw.put(13L, values(dates, 20, 20, 20, 20));
        Map<Long, Map<LocalDate, JavaZScoreCandidate.Cell>> deltas = new LinkedHashMap<>();
        deltas.put(11L, values(dates, null, 1.1, 12.0 / 11.0, 13.0 / 12.0));
        deltas.put(13L, values(dates, null, 1.0, 1.0, 1.0));
        Map<LocalDate, JavaZScoreCandidate.Evidence> evidence = new LinkedHashMap<>();
        for (LocalDate date : dates) evidence.put(date, new JavaZScoreCandidate.Evidence(true, "id-" + date, "id-" + date, "receipt"));

        List<JavaZScoreCandidate.ScoreRow> price = JavaZScoreCandidate.calculate(
                JavaZScoreCandidate.Family.PRICE, 3, dates.get(3), dates, tickers, raw, Map.of(), evidence);
        List<JavaZScoreCandidate.ScoreRow> delta = JavaZScoreCandidate.calculate(
                JavaZScoreCandidate.Family.PRICE_DELTA, 3, dates.get(3), dates, tickers, raw, deltas, evidence);

        assertEquals(List.of(11L, 13L), price.stream().map(JavaZScoreCandidate.ScoreRow::tickerId).toList());
        assertEquals(JavaZScoreCandidate.Status.READY, price.get(0).status());
        assertNotNull(price.get(0).score());
        assertEquals(JavaZScoreCandidate.Status.ZERO_VARIANCE, price.get(1).status());
        assertEquals(JavaZScoreCandidate.Status.READY, delta.get(0).status());
        assertEquals(JavaZScoreCandidate.Status.ZERO_VARIANCE, delta.get(1).status());
    }

    @Test
    void unavailableInputsUsePrecedenceAndKeepScoresNull() {
        List<LocalDate> dates = dates(3);
        Map<Long, String> tickers = Map.of(11L, "AAP");
        Map<Long, Map<LocalDate, JavaZScoreCandidate.Cell>> raw = new LinkedHashMap<>();
        Map<LocalDate, JavaZScoreCandidate.Cell> rawValues = new LinkedHashMap<>();
        rawValues.put(dates.get(0), new JavaZScoreCandidate.Cell(10.0));
        rawValues.put(dates.get(1), new JavaZScoreCandidate.Cell(null));
        rawValues.put(dates.get(2), new JavaZScoreCandidate.Cell(12.0));
        raw.put(11L, rawValues);
        Map<LocalDate, JavaZScoreCandidate.Evidence> evidence = new LinkedHashMap<>();
        evidence.put(dates.get(0), new JavaZScoreCandidate.Evidence(true, "a", "a", "receipt"));
        evidence.put(dates.get(1), new JavaZScoreCandidate.Evidence(false, "b", null, null));
        evidence.put(dates.get(2), new JavaZScoreCandidate.Evidence(true, "c", "c", "receipt"));

        List<JavaZScoreCandidate.ScoreRow> rows = JavaZScoreCandidate.calculate(
                JavaZScoreCandidate.Family.PRICE, 3, dates.get(2), dates, tickers, raw, Map.of(), evidence);

        assertEquals(JavaZScoreCandidate.Status.UNVERIFIED_INPUT, rows.get(0).status());
        assertEquals(null, rows.get(0).score());
    }

    @Test
    void float32QuantizationIsFiniteAndStable() {
        float value = JavaZScoreCandidate.quantizeFloat32(1.0 / 3.0);
        assertEquals((float) (1.0 / 3.0), value);
    }

    @Test
    void largeOffsetSmallVarianceMatchesScalarPythonReference() {
        List<LocalDate> dates = dates(61);
        double[] prices = new double[61];
        for (int index = 0; index < prices.length; index++) prices[index] = 1e12 + index * 0.001;
        Map<Long, Map<LocalDate, JavaZScoreCandidate.Cell>> raw = Map.of(11L, values(dates, prices));
        Map<LocalDate, JavaZScoreCandidate.Evidence> evidence = new LinkedHashMap<>();
        for (LocalDate date : dates) evidence.put(date, new JavaZScoreCandidate.Evidence(true, "id", "id", "receipt"));
        for (int window : new int[]{20, 60}) {
            List<JavaZScoreCandidate.ScoreRow> rows = JavaZScoreCandidate.calculate(
                    JavaZScoreCandidate.Family.PRICE, window, dates.get(60), dates, Map.of(11L, "AAP"), raw, Map.of(), evidence);
            double expected = window == 20 ? 1.6495943314231643 : 1.7056289244465483;
            assertEquals(expected, rows.getFirst().score(), 1e-6);
        }
    }

    @Test
    void invalidDeltaDependenciesOutrankMissingInputsAndShortHistoryIsExplicit() {
        List<LocalDate> dates = dates(4);
        Map<LocalDate, JavaZScoreCandidate.Evidence> evidence = new LinkedHashMap<>();
        for (LocalDate date : dates) evidence.put(date, new JavaZScoreCandidate.Evidence(true, "id", "id", "receipt"));
        Map<Long, Map<LocalDate, JavaZScoreCandidate.Cell>> raw = Map.of(
                11L, values(dates, 10, -2, 12, 13),
                13L, values(dates, 10, 11, 12, 13));
        Map<Long, Map<LocalDate, JavaZScoreCandidate.Cell>> deltas = Map.of(
                11L, values(dates, null, 1.1, 1.1, 13.0 / 12),
                13L, values(dates, null, 1.1, 99, 13.0 / 12));
        List<JavaZScoreCandidate.ScoreRow> invalid = JavaZScoreCandidate.calculate(
                JavaZScoreCandidate.Family.PRICE_DELTA, 3, dates.getLast(), dates,
                Map.of(11L, "AAP", 13L, "AAp"), raw, deltas, evidence);
        for (JavaZScoreCandidate.ScoreRow row : invalid) {
            assertEquals(JavaZScoreCandidate.Status.INVALID_INPUT, row.status());
            assertEquals(null, row.score());
        }
        List<JavaZScoreCandidate.ScoreRow> shortHistory = JavaZScoreCandidate.calculate(
                JavaZScoreCandidate.Family.PRICE, 20, dates.getLast(), dates,
                Map.of(13L, "AAp"), raw, Map.of(), evidence);
        assertEquals(JavaZScoreCandidate.Status.INSUFFICIENT_HISTORY, shortHistory.getFirst().status());
    }

    private static List<LocalDate> dates(int count) {
        List<LocalDate> result = new ArrayList<>();
        for (int index = 0; index < count; index++) result.add(LocalDate.of(2024, 1, 2).plusDays(index));
        return result;
    }

    private static Map<LocalDate, JavaZScoreCandidate.Cell> values(List<LocalDate> dates, double... values) {
        Map<LocalDate, JavaZScoreCandidate.Cell> result = new LinkedHashMap<>();
        for (int index = 0; index < dates.size(); index++) result.put(dates.get(index), new JavaZScoreCandidate.Cell(values[index]));
        return result;
    }

    private static Map<LocalDate, JavaZScoreCandidate.Cell> values(List<LocalDate> dates, Double first, double... rest) {
        Map<LocalDate, JavaZScoreCandidate.Cell> result = new LinkedHashMap<>();
        result.put(dates.get(0), new JavaZScoreCandidate.Cell(first));
        for (int index = 1; index < dates.size(); index++) result.put(dates.get(index), new JavaZScoreCandidate.Cell(rest[index - 1]));
        return result;
    }
}
