package stockguidance.database;

import org.junit.jupiter.api.Test;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.SQLException;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ReadOnlyTransactionTest {
    @Test
    void configuresRepeatableReadAndRollsBackSuccessfulInspection() {
        ConnectionState state = new ConnectionState();
        DatabaseConfiguration configuration = new DatabaseConfiguration("localhost", "fixture_data", "fixture-user", "secret", 3306, DatabaseDriver.AUTO);
        Integer result = new ReadOnlyTransaction(ignored -> state.connection(), configuration).execute(connection -> 42);

        assertEquals(42, result);
        assertTrue(state.readOnly);
        assertEquals(Connection.TRANSACTION_REPEATABLE_READ, state.isolation);
        assertEquals(1, state.rollbackCount);
        assertTrue(state.closed);
    }

    @Test
    void rollsBackAndSanitizesInspectionFailure() {
        ConnectionState state = new ConnectionState();
        DatabaseConfiguration configuration = new DatabaseConfiguration("localhost", "fixture_data", "fixture-user", "secret-never-log", 3306, DatabaseDriver.AUTO);
        DatabaseAccessException error = assertThrows(DatabaseAccessException.class, () ->
                new ReadOnlyTransaction(ignored -> state.connection(), configuration).execute(connection -> {
                    throw new SQLException("secret-never-log");
                }));

        assertEquals("Read-only database inspection failed", error.getMessage());
        assertEquals(1, state.rollbackCount);
        assertTrue(state.closed);
    }

    private static final class ConnectionState implements InvocationHandler {
        private boolean readOnly;
        private int isolation;
        private int rollbackCount;
        private boolean closed;

        private Connection connection() {
            return (Connection) Proxy.newProxyInstance(Connection.class.getClassLoader(), new Class<?>[]{Connection.class}, this);
        }

        @Override
        public Object invoke(Object proxy, java.lang.reflect.Method method, Object[] arguments) {
            return switch (method.getName()) {
                case "setReadOnly" -> {
                    readOnly = (Boolean) arguments[0];
                    yield null;
                }
                case "setTransactionIsolation" -> {
                    isolation = (Integer) arguments[0];
                    yield null;
                }
                case "rollback" -> {
                    rollbackCount++;
                    yield null;
                }
                case "close" -> {
                    closed = true;
                    yield null;
                }
                case "isClosed" -> closed;
                case "toString" -> "fixture-connection";
                default -> defaultValue(method.getReturnType());
            };
        }

        private static Object defaultValue(Class<?> type) {
            if (type == boolean.class) {
                return false;
            }
            if (type == byte.class || type == short.class || type == int.class || type == long.class || type == float.class || type == double.class) {
                return 0;
            }
            if (type == char.class) {
                return '\0';
            }
            return null;
        }
    }
}
