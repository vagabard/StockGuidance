package stockguidance.database;

import org.junit.jupiter.api.Test;

import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.time.LocalDate;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class SqlIdentifiersTest {
    @Test
    void allowsOnlyKnownIdentifiers() {
        assertEquals("transaction_dates", SqlIdentifiers.requireAllowed("transaction_dates", "table", Set.of("transaction_dates")));
        assertThrows(IllegalArgumentException.class, () -> SqlIdentifiers.requireAllowed("transaction_dates; DROP TABLE users", "table", Set.of("transaction_dates")));
        assertThrows(IllegalArgumentException.class, () -> SqlIdentifiers.requireAllowed("other_table", "table", Set.of("transaction_dates")));
    }

    @Test
    void bindsDateValuesAsParameters() throws Exception {
        String[] sql = new String[1];
        Object[] firstParameter = new Object[1];
        Object[] secondParameter = new Object[1];
        PreparedStatement statement = (PreparedStatement) Proxy.newProxyInstance(
                PreparedStatement.class.getClassLoader(),
                new Class<?>[]{PreparedStatement.class},
                (proxy, method, arguments) -> {
                    if (method.getName().equals("setObject")) {
                        if ((Integer) arguments[0] == 1) {
                            firstParameter[0] = arguments[1];
                        } else {
                            secondParameter[0] = arguments[1];
                        }
                    }
                    return null;
                });
        Connection connection = (Connection) Proxy.newProxyInstance(
                Connection.class.getClassLoader(),
                new Class<?>[]{Connection.class},
                (proxy, method, arguments) -> {
                    if (method.getName().equals("prepareStatement")) {
                        sql[0] = (String) arguments[0];
                        return statement;
                    }
                    return null;
                });

        PreparedDateRangeQuery.prepare(connection, "transaction_dates", "transaction_date", LocalDate.of(2024, 1, 1), LocalDate.of(2024, 1, 31));
        assertEquals(LocalDate.of(2024, 1, 1), firstParameter[0]);
        assertEquals(LocalDate.of(2024, 1, 31), secondParameter[0]);
        assertEquals(true, sql[0].contains("?") && sql[0].contains("transaction_dates"));
    }
}
