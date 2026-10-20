package stockguidance.validation;

import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import stockguidance.json.CanonicalJson;

import java.math.BigDecimal;
import java.util.Comparator;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

public final class ValidationContent {
    private ValidationContent() {
    }

    public static String rawId(Map<String, RawBar> bars) {
        ArrayNode rows = JsonNodeFactory.instance.arrayNode();
        bars.values().stream().sorted(Comparator.comparing(RawBar::ticker)).forEach(bar -> {
            ObjectNode value = JsonNodeFactory.instance.objectNode();
            value.put("T", bar.ticker());
            value.put("o", bar.open().toPlainString());
            value.put("h", bar.high().toPlainString());
            value.put("l", bar.low().toPlainString());
            value.put("c", bar.close().toPlainString());
            value.put("v", bar.volume().stripTrailingZeros().toPlainString());
            if (bar.vwap() == null) {
                value.putNull("vw");
            } else {
                value.put("vw", bar.vwap().toPlainString());
            }
            if (bar.transactions() == null) {
                value.putNull("n");
            } else {
                value.put("n", bar.transactions());
            }
            rows.add(value);
        });
        return CanonicalJson.sha256(rows);
    }

    public static String derivedId(Map<String, DerivedRow> rows) {
        ArrayNode values = JsonNodeFactory.instance.arrayNode();
        rows.values().stream().sorted(Comparator.comparing(DerivedRow::ticker)).forEach(row -> {
            ArrayNode item = values.addArray();
            item.add(row.ticker());
            for (Double value : row.values()) {
                if (value == null) {
                    item.addNull();
                } else {
                    item.add(value);
                }
            }
        });
        return CanonicalJson.sha256(values);
    }

    public static String dependencyId(String priorRawId, String currentRawId, String followingRawId) {
        ArrayNode values = JsonNodeFactory.instance.arrayNode();
        values.add("legacy-open-to-close-v1");
        values.add(priorRawId);
        values.add(currentRawId);
        values.add(followingRawId);
        return CanonicalJson.sha256(values);
    }

    public record RawBar(String ticker, BigDecimal open, BigDecimal high, BigDecimal low, BigDecimal close,
                         BigDecimal volume, BigDecimal vwap, Long transactions) {
        public RawBar(String ticker, BigDecimal open, BigDecimal high, BigDecimal low, BigDecimal close,
                      long volume, BigDecimal vwap, Long transactions) {
            this(ticker, open, high, low, close, BigDecimal.valueOf(volume), vwap, transactions);
        }
    }

    public record DerivedRow(String ticker, List<Double> values) {
        public DerivedRow {
            values = Collections.unmodifiableList(new ArrayList<>(values));
        }
    }
}
