package stockguidance.validation;

import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import stockguidance.json.CanonicalJson;

import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

final class LegacyDerivedCalculator {
    private static final MathContext PYTHON_DECIMAL_CONTEXT = new MathContext(28, RoundingMode.HALF_EVEN);
    static final List<String> COLUMNS = List.of(
            "open_ratio_to_prior", "high_ratio_to_prior", "low_ratio_to_prior", "close_ratio_to_prior",
            "volume_ratio_to_prior", "vwap_ratio_to_prior", "transactions_ratio_to_prior", "change_in_price_over_day",
            "next_day_change_in_price", "zero_centered_next_day_price_change", "next_day_price_change_to_extremes_0_1",
            "next_day_raise1_lower0", "next_day_top100_raise1_lower0", "zero_center_extremes_neg1_pos1",
            "zero_center_top100_1_other_neg1", "zero_center_raise1_lower_neg1");

    private LegacyDerivedCalculator() {
    }

    static Map<String, List<Double>> expected(
            Map<String, ValidationContent.RawBar> prior,
            Map<String, ValidationContent.RawBar> current,
            Map<String, ValidationContent.RawBar> following) {
        Map<String, Double> scores = new HashMap<>();
        for (Map.Entry<String, ValidationContent.RawBar> item : current.entrySet()) {
            ValidationContent.RawBar next = following.get(item.getKey());
            if (next != null) {
                scores.put(item.getKey(), next.close().divide(next.open(), PYTHON_DECIMAL_CONTEXT).doubleValue());
            }
        }
        List<String> ranked = scores.keySet().stream()
                .sorted(Comparator.<String>comparingDouble(scores::get).reversed().thenComparing(Comparator.naturalOrder()))
                .toList();
        Set<String> top = new HashSet<>(ranked.subList(0, Math.min(100, ranked.size())));
        Map<String, List<Double>> result = new TreeMap<>();
        for (Map.Entry<String, ValidationContent.RawBar> item : current.entrySet()) {
            String ticker = item.getKey();
            ValidationContent.RawBar bar = item.getValue();
            ValidationContent.RawBar previous = prior.get(ticker);
            ValidationContent.RawBar next = following.get(ticker);
            List<Double> values = new ArrayList<>();
            values.add(ratio(bar.open(), previous == null ? null : previous.open()));
            values.add(ratio(bar.high(), previous == null ? null : previous.high()));
            values.add(ratio(bar.low(), previous == null ? null : previous.low()));
            values.add(ratio(bar.close(), previous == null ? null : previous.close()));
            values.add(ratio(BigDecimal.valueOf(bar.volume()), previous == null ? null : BigDecimal.valueOf(previous.volume())));
            values.add(ratio(bar.vwap(), previous == null ? null : previous.vwap()));
            values.add(ratio(bar.transactions() == null ? null : BigDecimal.valueOf(bar.transactions()), previous == null || previous.transactions() == null ? null : BigDecimal.valueOf(previous.transactions())));
            values.add(ratio(bar.close(), bar.open()));
            Double outcome = next == null ? null : ratio(next.close(), next.open());
            values.add(outcome);
            Double centered = outcome == null ? null : outcome - 1.0;
            values.add(centered);
            Double extreme = outcome == null ? null : (outcome >= 1.0 ? 0.5 : 0.0) + outcome / (2.0 * (outcome + 1.0));
            values.add(extreme);
            Double raise = outcome == null ? null : outcome >= 1.0 ? 1.0 : 0.0;
            values.add(raise);
            Double topFlag = outcome == null ? null : top.contains(ticker) ? 1.0 : 0.0;
            values.add(topFlag);
            values.add(extreme == null ? null : extreme * 2.0 - 1.0);
            values.add(topFlag == null ? null : topFlag * 2.0 - 1.0);
            values.add(raise == null ? null : raise * 2.0 - 1.0);
            result.put(ticker, values);
        }
        return result;
    }

    static List<Integer> differingColumns(List<Double> actual, List<Double> expected) {
        List<Integer> result = new ArrayList<>();
        for (int index = 0; index < COLUMNS.size(); index++) {
            if (!equal(actual.get(index), expected.get(index))) result.add(index);
        }
        return result;
    }

    static String studyId(Map<String, List<Double>> rows) {
        ArrayNode values = JsonNodeFactory.instance.arrayNode();
        rows.keySet().stream().sorted().forEach(ticker -> {
            ArrayNode item = values.addArray();
            item.add(ticker);
            for (Double value : rows.get(ticker)) {
                if (value == null) item.addNull();
                else item.add(canonical(value));
            }
        });
        return CanonicalJson.sha256(values);
    }

    static Map<String, List<Double>> values(Map<String, ValidationContent.DerivedRow> rows) {
        Map<String, List<Double>> result = new TreeMap<>();
        rows.forEach((ticker, row) -> result.put(ticker, row.values()));
        return result;
    }

    private static Double ratio(BigDecimal numerator, BigDecimal denominator) {
        return numerator == null || denominator == null || denominator.signum() == 0
                ? null
                : numerator.divide(denominator, PYTHON_DECIMAL_CONTEXT).doubleValue();
    }

    private static boolean equal(Double observed, Double required) {
        if (observed == null || required == null) return observed == required;
        return Double.isFinite(observed) && Math.abs(observed - required) <= Math.max(1e-12, 1e-10 * Math.max(Math.abs(observed), Math.abs(required)));
    }

    private static String canonical(double value) {
        if (value == 0.0d) return "0";
        return BigDecimal.valueOf(value).stripTrailingZeros().toPlainString();
    }
}
