package stockguidance.database;

import java.sql.Connection;
import java.sql.SQLException;

public final class ReadOnlyTransaction {
    @FunctionalInterface
    public interface Work<T> {
        T run(Connection connection) throws SQLException;
    }

    private final ConnectionFactory factory;
    private final DatabaseConfiguration configuration;

    public ReadOnlyTransaction(ConnectionFactory factory, DatabaseConfiguration configuration) {
        this.factory = factory;
        this.configuration = configuration;
    }

    public <T> T execute(Work<T> work) {
        if (factory == null || configuration == null || work == null) {
            throw new DatabaseAccessException("Read-only transaction requires explicit configuration");
        }
        try (Connection connection = factory.open(configuration)) {
            connection.setAutoCommit(false);
            connection.setReadOnly(true);
            connection.setTransactionIsolation(Connection.TRANSACTION_REPEATABLE_READ);
            try {
                T result = work.run(connection);
                connection.rollback();
                return result;
            } catch (SQLException | RuntimeException error) {
                rollback(connection);
                if (error instanceof DatabaseAccessException databaseAccessException) {
                    throw databaseAccessException;
                }
                throw new DatabaseAccessException("Read-only database inspection failed");
            }
        } catch (SQLException | RuntimeException error) {
            if (error instanceof DatabaseAccessException databaseAccessException) {
                throw databaseAccessException;
            }
            throw new DatabaseAccessException("Read-only database inspection failed");
        }
    }

    private static void rollback(Connection connection) {
        try {
            connection.rollback();
        } catch (SQLException ignored) {
        }
    }
}
