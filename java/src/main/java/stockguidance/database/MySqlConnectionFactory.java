package stockguidance.database;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.Properties;

public final class MySqlConnectionFactory implements ConnectionFactory {
    @Override
    public Connection open(DatabaseConfiguration configuration) {
        if (configuration == null) {
            throw new DatabaseAccessException("Configured MySQL connection failed");
        }
        try {
            String host = configuration.host();
            if (host.chars().anyMatch(Character::isWhitespace) || host.indexOf('\n') >= 0 || host.indexOf('\r') >= 0) {
                throw new SQLException("Invalid host");
            }
            String database = SqlIdentifiers.require(configuration.database(), "database");
            String url = "jdbc:mysql://" + host + ":" + configuration.port() + "/" + database;
            Properties properties = new Properties();
            properties.setProperty("user", configuration.user());
            properties.setProperty("password", configuration.password());
            properties.setProperty("useUnicode", "true");
            properties.setProperty("characterEncoding", "UTF-8");
            properties.setProperty("useCursorFetch", "true");
            return DriverManager.getConnection(url, properties);
        } catch (SQLException | RuntimeException error) {
            throw new DatabaseAccessException("Configured MySQL connection failed");
        }
    }
}
