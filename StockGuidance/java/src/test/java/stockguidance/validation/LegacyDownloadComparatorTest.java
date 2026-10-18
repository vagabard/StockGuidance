package stockguidance.validation;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;
import stockguidance.database.ConnectionFactory;
import stockguidance.database.DatabaseConfiguration;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.Date;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.LocalDate;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;

class LegacyDownloadComparatorTest {
    @Test
    void exactDownloadAndDatabaseRowsAreComplete() throws Exception {
        Path root = Files.createTempDirectory("legacy-download");
        writeDownload(root, "2024-07-01", "10.0000");
        JsonNode report = new LegacyDownloadComparator(new FixtureDatabase(false)).compare(configuration(), root, null, null).report();
        assertEquals(0, report.get("exit_code").intValue());
        assertEquals(0, report.get("finding_count").intValue());
    }

    @Test
    void valueDifferenceIsReportedWithoutAWrite() throws Exception {
        Path root = Files.createTempDirectory("legacy-download");
        writeDownload(root, "2024-07-01", "12.0000");
        JsonNode report = new LegacyDownloadComparator(new FixtureDatabase(false)).compare(configuration(), root, null, null).report();
        assertEquals(2, report.get("exit_code").intValue());
        assertEquals("value", report.get("findings").get(0).get("check").textValue());
    }

    @Test
    void malformedRecordIsReportedAsInvalid() throws Exception {
        Path root = Files.createTempDirectory("legacy-download");
        Path directory = root.resolve("market_summary").resolve("2024");
        Files.createDirectories(directory);
        Files.writeString(directory.resolve("2024-07-01.ndjson"), "{\"T\":\"AAA\"}\n");
        JsonNode report = new LegacyDownloadComparator(new FixtureDatabase(true)).compare(configuration(), root, null, null).report();
        assertEquals(2, report.get("exit_code").intValue());
        assertEquals("invalid", report.get("findings").get(0).get("category").textValue());
    }

    private static DatabaseConfiguration configuration() {
        return new DatabaseConfiguration("localhost", "fixture", "user", "secret", 3306, stockguidance.database.DatabaseDriver.AUTO);
    }

    private static void writeDownload(Path root, String date, String close) throws Exception {
        Path directory = root.resolve("market_summary").resolve("2024");
        Files.createDirectories(directory);
        long timestamp = Instant.parse(date + "T20:00:00Z").toEpochMilli();
        Files.writeString(directory.resolve(date + ".ndjson"), "{\"T\":\"AAA\",\"v\":100,\"vw\":10,\"o\":10,\"c\":" + close + ",\"h\":12,\"l\":9,\"t\":" + timestamp + ",\"n\":5}\n");
    }

    private static final class FixtureDatabase implements ConnectionFactory {
        private final boolean emptyRaw;

        private FixtureDatabase(boolean emptyRaw) {
            this.emptyRaw = emptyRaw;
        }

        @Override
        public Connection open(DatabaseConfiguration ignored) {
            InvocationHandler handler = (proxy, method, arguments) -> switch (method.getName()) {
                case "prepareStatement" -> prepared((String) arguments[0]);
                case "setAutoCommit", "setReadOnly", "setTransactionIsolation", "rollback", "close" -> null;
                default -> defaultValue(method.getReturnType());
            };
            return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[]{Connection.class}, handler);
        }

        private PreparedStatement prepared(String sql) {
            InvocationHandler handler = (proxy, method, arguments) -> {
                if (method.getName().equals("executeQuery")) {
                    if (sql.toLowerCase().contains("raw_stock_data")) {
                        return resultSet(emptyRaw ? List.of() : List.<Object[]>of(new Object[]{Date.valueOf("2024-07-01"), "AAA", new BigDecimal("10.0000"), new BigDecimal("12.0000"), new BigDecimal("9.0000"), new BigDecimal("10.0000"), 100L, new BigDecimal("10.0000"), 5L}));
                    }
                    return resultSet(sql.toLowerCase().contains("transaction_dates") ? List.<Object[]>of(new Object[]{Date.valueOf("2024-07-01")}) : List.of());
                }
                return defaultValue(method.getReturnType());
            };
            return (PreparedStatement) Proxy.newProxyInstance(PreparedStatement.class.getClassLoader(), new Class<?>[]{PreparedStatement.class}, handler);
        }

        private ResultSet resultSet(List<Object[]> values) {
            class State { int index = -1; boolean wasNull; }
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
            if (value == null) return method.equals("getLong") ? 0L : null;
            return switch (method) {
                case "getLong" -> ((Number) value).longValue();
                case "getBigDecimal" -> value instanceof BigDecimal ? value : new BigDecimal(value.toString());
                case "getString" -> value.toString();
                case "getDate" -> value;
                default -> throw new SQLException("Unsupported fixture result accessor");
            };
        }

        private static Object defaultValue(Class<?> type) {
            if (type == boolean.class) return false;
            if (type == int.class || type == long.class) return 0;
            return null;
        }
    }
}
