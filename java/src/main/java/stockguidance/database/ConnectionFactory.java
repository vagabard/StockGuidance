package stockguidance.database;

import java.sql.Connection;

@FunctionalInterface
public interface ConnectionFactory {
    Connection open(DatabaseConfiguration configuration);
}
