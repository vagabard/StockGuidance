package stockguidance.migration;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import stockguidance.database.TableNames;
import stockguidance.json.JsonSupport;

import java.lang.reflect.Proxy;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.Date;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.time.LocalDate;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

class OperationalDatabaseTest {
    @TempDir Path directory;

    @Test
    void planningTrustsCommittedHistoryAndFindsNewChangedAndMissingInputs() {
        var receipt = new OperationalDatabase.Receipt("identity", "receipt", "dependency", "derived");
        var old = new OperationalDatabase.Fingerprint(100, 200);
        var changed = new OperationalDatabase.Fingerprint(101, 201);
        assertEquals(OperationalDatabase.InputAction.TRUSTED, OperationalDatabase.planInput(receipt, old, old, false));
        assertEquals(OperationalDatabase.InputAction.TRUSTED, OperationalDatabase.planInput(receipt, old, null, false));
        assertEquals(OperationalDatabase.InputAction.BASELINE, OperationalDatabase.planInput(receipt, null, old, false));
        assertEquals(OperationalDatabase.InputAction.IMPORT, OperationalDatabase.planInput(receipt, old, changed, false));
        assertEquals(OperationalDatabase.InputAction.IMPORT, OperationalDatabase.planInput(receipt, old, old, true));
        assertEquals(OperationalDatabase.InputAction.IMPORT, OperationalDatabase.planInput(null, old, old, false));
        assertEquals(OperationalDatabase.InputAction.MISSING, OperationalDatabase.planInput(null, null, null, false));
        assertEquals(OperationalDatabase.InputAction.MISSING, OperationalDatabase.planInput(receipt, old, null, true));
    }

    @Test
    void filePlanningDoesNotParseCompletedFiles() throws Exception {
        Path file = directory.resolve("2024-07-01.ndjson");
        Files.writeString(file, "This is deliberately not JSON");
        var fingerprint = OperationalDatabase.Fingerprint.read(file);
        var receipt = new OperationalDatabase.Receipt("historical", "receipt", null, null);
        assertEquals(OperationalDatabase.InputAction.TRUSTED, OperationalDatabase.planInput(receipt, fingerprint, fingerprint, false));
        Files.delete(file);
        assertEquals(OperationalDatabase.InputAction.TRUSTED, OperationalDatabase.planInput(receipt, fingerprint, null, false));
    }

    @Test
    void databaseDerivedInputsPreserveDecimalsAndShareOneQuery() throws Exception {
        LocalDate session = LocalDate.of(2024, 7, 1);
        LocalDate following = session.plusDays(1);
        BigDecimal exact = new BigDecimal("1702500000000000.12345678");
        AtomicInteger queries = new AtomicInteger();
        Connection connection = connection(List.<Object[]>of(new Object[]{Date.valueOf(session), "AAp", exact, exact, exact, exact,
                new BigDecimal("123.00000001"), new BigDecimal("10.1234567890123456789012"), null}), queries);
        TableNames names = TableNames.decode(JsonSupport.read(Path.of("../src/stockguidance/database/table_names.json")));
        var receipts = Map.of(session, new OperationalDatabase.Receipt("historical-id", "receipt", null, null));
        var inputs = OperationalDatabase.readRaw(connection, names, List.of(session, following), receipts);
        assertEquals(1, queries.get());
        var current = inputs.get(session);
        assertNull(current.path());
        assertEquals("historical-id", current.recordsId());
        assertEquals(exact, current.bars().get("AAp").close());
        assertEquals(new BigDecimal("123.00000001"), current.bars().get("AAp").volume());
        assertNull(current.bars().get("AAp").transactions());
        assertTrue(inputs.get(following).bars().isEmpty());
        assertEquals(ArchiveCandidateReader.recordsId(Map.of()), inputs.get(following).recordsId());
    }

    @Test
    void committedReceiptWithMissingRowsFailsWhenCalculationNeedsIt() throws Exception {
        LocalDate session = LocalDate.of(2024, 7, 1);
        TableNames names = TableNames.decode(JsonSupport.read(Path.of("../src/stockguidance/database/table_names.json")));
        var receipts = Map.of(session, new OperationalDatabase.Receipt("identity", "receipt", null, null));
        assertThrows(IllegalArgumentException.class, () -> OperationalDatabase.readRaw(connection(List.of(), new AtomicInteger()),
                names, List.of(session), receipts));
    }

    private Connection connection(List<Object[]> values, AtomicInteger queries) {
        return (Connection) Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{Connection.class},
                (proxy, method, arguments) -> {
                    if (!method.getName().equals("prepareStatement")) return null;
                    assertTrue(arguments[0].toString().contains("JOIN `ticker_symbol`"));
                    return Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{PreparedStatement.class},
                            (statement, action, parameters) -> {
                                if (!action.getName().equals("executeQuery")) return null;
                                queries.incrementAndGet();
                                AtomicInteger position = new AtomicInteger(-1);
                                boolean[] wasNull = {false};
                                return Proxy.newProxyInstance(getClass().getClassLoader(), new Class<?>[]{ResultSet.class},
                                        (rows, operation, columns) -> {
                                            if (operation.getName().equals("next")) return position.incrementAndGet() < values.size();
                                            if (operation.getName().equals("wasNull")) return wasNull[0];
                                            if (!operation.getName().startsWith("get")) return null;
                                            Object value = values.get(position.get())[(int) columns[0] - 1];
                                            wasNull[0] = value == null;
                                            return operation.getName().equals("getLong") && value == null ? 0L : value;
                                        });
                            });
                });
    }
}
