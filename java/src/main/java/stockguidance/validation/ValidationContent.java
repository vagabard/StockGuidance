package stockguidance.validation;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
import stockguidance.json.JsonSupport;


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
        JsonArray rows = JsonSupport.array();
        bars.values().stream().sorted(Comparator.comparing(RawBar::ticker)).forEach(bar -> {
            JsonObject value = JsonSupport.object();
            value.put("T", bar.ticker());
            value.put("o", bar.open().toPlainString());
            value.put("h", bar.high().toPlainString());
            value.put("l", bar.low().toPlainString());
            value.put("c", bar.close().toPlainString());
            value.put("v", bar.volume().stripTrailingZeros().toPlainString());
            if (bar.vwap() == null) {
                value.put("vw", null);
            } else {
                value.put("vw", bar.vwap().toPlainString());
            }
            if (bar.transactions() == null) {
                value.put("n", null);
            } else {
                value.put("n", bar.transactions());
            }
            rows.add(value);
        });
        return CanonicalJson.sha256(rows);
    }

    public static String derivedId(Map<String, DerivedRow> rows) {
        JsonArray values = JsonSupport.array();
        rows.values().stream().sorted(Comparator.comparing(DerivedRow::ticker)).forEach(row -> {
            JsonArray item = JsonSupport.addArray(values);
            item.add(row.ticker());
            for (Double value : row.values()) {
                if (value == null) {
                    item.add(null);
                } else {
                    item.add(value);
                }
            }
        });
        return CanonicalJson.sha256(values);
    }

    public static String dependencyId(String priorRawId, String currentRawId, String followingRawId) {
        JsonArray values = JsonSupport.array();
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
