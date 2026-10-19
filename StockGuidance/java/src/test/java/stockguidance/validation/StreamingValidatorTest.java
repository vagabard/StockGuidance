package stockguidance.validation;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;
import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.TableNames;
import stockguidance.json.JsonSupport;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class StreamingValidatorTest {
    @Test
    void cleanBoundedReconciliationUsesCalendarChronologyAndRollsBack() {
        Fixture fixture = new Fixture();
        JsonNode report = fixture.validator().validate(fixture.configuration(), fixture.request);

        assertTrue(report.get("complete").booleanValue());
        assertEquals(0, report.get("exit_code").intValue());
        assertEquals(List.of("AAA"), textValues(report.get("sessions").get(0).get("raw_valid_keys")));
        assertEquals(1, fixture.database.rollbackCount);
        assertEquals(0, fixture.database.commitCount);
        assertTrue(fixture.database.statements.stream().allMatch(item -> item.startsWith("SELECT")));
    }

    @Test
    void nullableValuesRemainNullableAndDoNotBecomeZero() {
        Fixture fixture = new Fixture();
        JsonNode report = fixture.validator().validate(fixture.configuration(), fixture.request);

        assertTrue(report.get("complete").booleanValue());
        assertEquals(0, report.get("findings").size());
    }

    @Test
    void duplicateAndOrphanRowsAreIndependentOfDecodedCounts() {
        Fixture fixture = new Fixture();
        fixture.rawRows.add(fixture.rawRow(10, 1, LocalDate.of(2024, 7, 2), "AAA"));
        fixture.rawRows.add(fixture.rawRow(999, 1, null, "AAA"));
        JsonNode report = fixture.validator().validate(fixture.configuration(), fixture.request);

        assertFalse(report.get("complete").booleanValue());
        assertTrue(hasCheck(report, "duplicate"));
        assertTrue(hasCheck(report, "orphan"));
        assertEquals(2, report.get("sessions").get(0).get("raw_count").intValue());
    }

    @Test
    void lateReceiptAndMissingMappingDoNotReadFutureOrUseIdsAsChronology() {
        Fixture late = new Fixture();
        late.receiptRows.set(1, late.receiptRow(LocalDate.of(2024, 7, 2), late.rawIds.get(LocalDate.of(2024, 7, 2)), "2024-07-06T21:00:00Z", late.dependencyId, late.derivedId));
        JsonNode lateReport = late.validator().validate(late.configuration(), late.request);
        assertEquals("not_yet_available", lateReport.get("sessions").get(0).get("state").textValue());

        Fixture gap = new Fixture();
        gap.dateRows.removeIf(row -> row[1].equals(java.sql.Date.valueOf("2024-07-02")));
        JsonNode gapReport = gap.validator().validate(gap.configuration(), gap.request);
        assertTrue(hasCategory(gapReport, "missing"));
        assertTrue(hasCheck(gapReport, "session"));
    }

    @Test
    void corruptDerivedValueIsReportedWithARepairProposal() {
        Fixture fixture = new Fixture();
        Object[] row = fixture.derivedRows.get(0);
        row[5] = 9.0;
        JsonNode report = fixture.validator().validate(fixture.configuration(), fixture.request);

        assertFalse(report.get("complete").booleanValue());
        assertTrue(hasCheck(report, "legacy_derived"));
        assertEquals("rebuild_legacy_derived", report.get("repair_plan").get("items").get(0).get("action").textValue());
    }

    @Test
    void customRegistryIsUsedForEveryValidatorTable() {
        TableNames names = TableNames.decode(JsonSupport.parse("{" +
                "\"schema_version\":1,\"tables\":{" +
                "\"source\":\"sg_source\",\"sessions\":\"sg_sessions\",\"tickers\":\"sg_tickers\"," +
                "\"prices\":\"sg_prices\",\"deltas\":\"sg_deltas\",\"imports\":\"sg_imports\"," +
                "\"zscore_series\":\"sg_zscore_series\",\"zscore_sessions\":\"sg_zscore_sessions\"}," +
                "\"zscore_templates\":{\"price\":\"sg_price_{window}\",\"price_delta\":\"sg_delta_{window}\"}}"));
        Fixture fixture = new Fixture(names);

        JsonNode report = fixture.validator().validate(fixture.configuration(), fixture.request);

        assertTrue(report.get("complete").booleanValue());
        assertTrue(fixture.database.statements.stream().anyMatch(item -> item.contains("sg_source")));
        assertTrue(fixture.database.statements.stream().anyMatch(item -> item.contains("sg_sessions")));
        assertTrue(fixture.database.statements.stream().anyMatch(item -> item.contains("sg_prices")));
        assertTrue(fixture.database.statements.stream().anyMatch(item -> item.contains("sg_deltas")));
        assertTrue(fixture.database.statements.stream().anyMatch(item -> item.contains("sg_imports")));
    }

    private static boolean hasCheck(JsonNode report, String check) {
        for (JsonNode finding : report.get("findings")) {
            if (finding.get("check").textValue().equals(check)) return true;
        }
        return false;
    }

    private static boolean hasCategory(JsonNode report, String category) {
        for (JsonNode finding : report.get("findings")) {
            if (finding.get("category").textValue().equals(category)) return true;
        }
        return false;
    }

    private static List<String> textValues(JsonNode values) {
        List<String> result = new ArrayList<>();
        for (JsonNode value : values) result.add(value.textValue());
        return result;
    }

    private static final class Fixture {
        private final TableNames tableNames;
        private final LocalDate prior = LocalDate.of(2024, 7, 1);
        private final LocalDate current = LocalDate.of(2024, 7, 2);
        private final LocalDate following = LocalDate.of(2024, 7, 3);
        private final Map<LocalDate, String> rawIds = new HashMap<>();
        private final List<Object[]> dateRows = new ArrayList<>();
        private final List<Object[]> rawRows = new ArrayList<>();
        private final List<Object[]> derivedRows = new ArrayList<>();
        private final List<Object[]> receiptRows = new ArrayList<>();
        private final FixtureDatabase database = new FixtureDatabase(this);
        private final ValidationRequest request;
        private final String dependencyId;
        private final String derivedId;

        private Fixture() {
            this(TableNames.loadPackaged());
        }

        private Fixture(TableNames tableNames) {
            this.tableNames = tableNames;
            ValidationContent.RawBar bar = bar();
            for (LocalDate date : List.of(prior, current, following)) {
                Map<String, ValidationContent.RawBar> bars = Map.of("AAA", bar);
                rawIds.put(date, ValidationContent.rawId(bars));
            }
            dependencyId = ValidationContent.dependencyId(rawIds.get(prior), rawIds.get(current), rawIds.get(following));
            Map<String, ValidationContent.DerivedRow> derived = Map.of("AAA", new ValidationContent.DerivedRow("AAA", java.util.Arrays.asList(1.0, 1.0, 1.0, 1.0, 1.0, null, null, 1.0, 1.0, 0.0, 0.75, 1.0, 1.0, 0.5, 1.0, 1.0)));
            derivedId = ValidationContent.derivedId(derived);
            dateRows.add(new Object[]{30, java.sql.Date.valueOf(following)});
            dateRows.add(new Object[]{10, java.sql.Date.valueOf(current)});
            dateRows.add(new Object[]{20, java.sql.Date.valueOf(prior)});
            rawRows.add(rawRow(20, 1, prior, "AAA"));
            rawRows.add(rawRow(30, 1, following, "AAA"));
            rawRows.add(rawRow(10, 1, current, "AAA"));
            Object[] derivedRow = new Object[20];
            derivedRow[0] = 10;
            derivedRow[1] = 1;
            derivedRow[2] = java.sql.Date.valueOf(current);
            derivedRow[3] = "AAA";
            for (int index = 0; index < 16; index++) derivedRow[index + 4] = derived.get("AAA").values().get(index);
            derivedRows.add(derivedRow);
            receiptRows.add(receiptRow(prior, rawIds.get(prior), "2024-07-01T21:00:00Z", null, null));
            receiptRows.add(receiptRow(current, rawIds.get(current), "2024-07-02T21:00:00Z", dependencyId, derivedId));
            receiptRows.add(receiptRow(following, rawIds.get(following), "2024-07-03T21:00:00Z", null, null));
            String source = "\"source\":{\"provider\":\"fixture\",\"endpoint\":\"fixture-endpoint\",\"market\":\"us-stocks\",\"universe\":\"fixture-universe\",\"adjustment\":\"adjusted\",\"interval\":\"1d\"}";
            String evidence = "[{\"exchange_date\":\"2024-07-02\",\"state\":\"complete\",\"available_at\":\"2024-07-02T21:00:00Z\",\"keys\":[\"AAA\"],\"content_id\":\"" + rawIds.get(current) + "\",\"records_id\":\"" + rawIds.get(current) + "\",\"record_count\":1}]";
            request = ValidationRequest.decode(JsonSupport.parse("{" +
                    "\"schema_version\":1,\"operation\":\"raw_derived_reconciliation\"," + source + "," +
                    "\"calendar\":{\"id\":\"XNYS\",\"version\":\"fixture-1\",\"sessions\":[" +
                    session(prior, 0) + "," + session(current, 1) + "," + session(following, 2) + "]}," +
                    "\"scope\":{\"start\":\"2024-07-02\",\"end\":\"2024-07-02\",\"as_of\":\"2024-07-05T21:00:00Z\"}," +
                    "\"source_evidence\":" + evidence + "," +
                    "\"history\":{\"history_start\":\"2024-07-01\",\"history_provenance\":\"fixture\",\"unresolved_limits\":[]},\"max_rows\":1000}"));
        }

        private ValidationRequest.SessionSpec spec(LocalDate date, int sequence) {
            return new ValidationRequest.SessionSpec(date, sequence, Instant.parse(date + "T13:30:00Z"), Instant.parse(date + "T20:00:00Z"));
        }

        private String session(LocalDate date, int sequence) {
            return "{\"exchange_date\":\"" + date + "\",\"sequence\":" + sequence + ",\"opens_at\":\"" + date + "T13:30:00Z\",\"close_at\":\"" + date + "T20:00:00Z\"}";
        }

        private ValidationContent.RawBar bar() {
            return new ValidationContent.RawBar("AAA", new BigDecimal("10.0000"), new BigDecimal("11.0000"), new BigDecimal("9.0000"), new BigDecimal("10.0000"), 100, null, null);
        }

        private Object[] rawRow(int dateId, int tickerId, LocalDate date, String ticker) {
            ValidationContent.RawBar value = bar();
            return new Object[]{dateId, tickerId, date == null ? null : java.sql.Date.valueOf(date), ticker, value.open(), value.high(), value.low(), value.close(), value.volume(), value.vwap(), value.transactions()};
        }

        private Object[] receiptRow(LocalDate date, String rawId, String available, String dependency, String derived) {
            return new Object[]{java.sql.Date.valueOf(date), rawId, available, dependency, derived};
        }

        private StreamingValidator validator() {
            return new StreamingValidator(database, tableNames);
        }

        private DatabaseConfiguration configuration() {
            return new DatabaseConfiguration("localhost", "fixture_data", "fixture-user", "secret-never-log", 3306, stockguidance.database.DatabaseDriver.AUTO);
        }
    }

    private static final class FixtureDatabase implements stockguidance.database.ConnectionFactory {
        private final Fixture fixture;
        private final List<String> statements = new ArrayList<>();
        private int rollbackCount;
        private int commitCount;

        private FixtureDatabase(Fixture fixture) {
            this.fixture = fixture;
        }

        @Override
        public Connection open(DatabaseConfiguration ignored) {
            InvocationHandler handler = (proxy, method, arguments) -> switch (method.getName()) {
                case "prepareStatement" -> prepared((String) arguments[0]);
                case "setAutoCommit", "setReadOnly", "setTransactionIsolation", "rollback", "close" -> {
                    if (method.getName().equals("rollback")) rollbackCount++;
                    yield null;
                }
                case "commit" -> {
                    commitCount++;
                    yield null;
                }
                default -> defaultValue(method.getReturnType());
            };
            return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[]{Connection.class}, handler);
        }

        private PreparedStatement prepared(String sql) {
            statements.add(sql);
            InvocationHandler handler = new InvocationHandler() {
                @Override
                public Object invoke(Object proxy, Method method, Object[] arguments) {
                    if (method.getName().equals("executeQuery")) return resultSet(rowsFor(sql));
                    return defaultValue(method.getReturnType());
                }
            };
            return (PreparedStatement) Proxy.newProxyInstance(PreparedStatement.class.getClassLoader(), new Class<?>[]{PreparedStatement.class}, handler);
        }

        private List<Object[]> rowsFor(String sql) {
            String lower = sql.toLowerCase();
            if (lower.contains("information_schema")) return List.<Object[]>of(
                    new Object[]{fixture.tableNames.deltas(), "InnoDB"}, new Object[]{fixture.tableNames.prices(), "InnoDB"}, new Object[]{fixture.tableNames.imports(), "InnoDB"},
                    new Object[]{fixture.tableNames.source(), "InnoDB"}, new Object[]{fixture.tableNames.tickers(), "InnoDB"}, new Object[]{fixture.tableNames.sessions(), "InnoDB"});
            if (lower.contains("source_key")) return List.<Object[]>of(new Object[]{fixture.request.sourceKey()});
            if (lower.contains(fixture.tableNames.prices().toLowerCase())) return fixture.rawRows;
            if (lower.contains(fixture.tableNames.deltas().toLowerCase())) return fixture.derivedRows;
            if (lower.contains(fixture.tableNames.imports().toLowerCase())) return fixture.receiptRows;
            if (lower.contains(fixture.tableNames.sessions().toLowerCase())) return fixture.dateRows;
            throw new IllegalArgumentException("Unexpected fixture SQL");
        }

        private ResultSet resultSet(List<Object[]> values) {
            class State {
                int index = -1;
                boolean wasNull;
            }
            State state = new State();
            InvocationHandler handler = (proxy, method, arguments) -> {
                if (method.getName().equals("next")) return ++state.index < values.size();
                if (method.getName().equals("wasNull")) return state.wasNull;
                if (method.getName().equals("close")) return null;
                if (method.getName().startsWith("get")) {
                    Object value = values.get(state.index)[((Integer) arguments[0]) - 1];
                    state.wasNull = value == null;
                    return convert(method.getName(), value);
                }
                return defaultValue(method.getReturnType());
            };
            return (ResultSet) Proxy.newProxyInstance(ResultSet.class.getClassLoader(), new Class<?>[]{ResultSet.class}, handler);
        }

        private static Object convert(String method, Object value) throws SQLException {
            if (value == null) return switch (method) {
                case "getInt" -> 0;
                case "getLong" -> 0L;
                case "getDouble" -> 0.0;
                default -> null;
            };
            return switch (method) {
                case "getInt" -> ((Number) value).intValue();
                case "getLong" -> ((Number) value).longValue();
                case "getDouble" -> ((Number) value).doubleValue();
                case "getBigDecimal" -> value instanceof BigDecimal ? value : new BigDecimal(value.toString());
                case "getString" -> value.toString();
                case "getDate" -> value;
                default -> throw new SQLException("Unsupported fixture result accessor");
            };
        }

        private static Object defaultValue(Class<?> type) {
            if (type == boolean.class) return false;
            if (type == int.class || type == long.class || type == double.class || type == float.class) return 0;
            return null;
        }
    }
}
