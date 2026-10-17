package stockguidance.database;

import com.fasterxml.jackson.databind.JsonNode;
import stockguidance.json.ContractException;
import stockguidance.json.JsonSupport;

import java.nio.file.Path;
import java.util.HashSet;
import java.util.Iterator;
import java.util.Set;

public final class DatabaseConfiguration {
    private static final Set<String> ALLOWED_FIELDS = Set.of("host", "database", "schema", "user", "password", "port", "driver");

    private final String host;
    private final String database;
    private final String user;
    private final String password;
    private final int port;
    private final DatabaseDriver driver;

    public DatabaseConfiguration(String host, String database, String user, String password, int port, DatabaseDriver driver) {
        this.host = requireText(host, "host");
        this.database = requireText(database, "database");
        this.user = requireText(user, "user");
        this.password = password == null ? "" : password;
        if (port <= 0 || port > 65535) {
            throw new IllegalArgumentException("Invalid database port");
        }
        this.port = port;
        this.driver = driver == null ? DatabaseDriver.AUTO : driver;
    }

    public static DatabaseConfiguration load(Path path) {
        try {
            return decode(JsonSupport.read(path));
        } catch (ContractException | IllegalArgumentException error) {
            throw new IllegalArgumentException("Cannot read the explicit database JSON configuration");
        }
    }

    public static DatabaseConfiguration decode(JsonNode root) {
        if (root == null || !root.isObject()) {
            throw new ContractException("Database configuration must be a JSON object");
        }
        Set<String> fields = new HashSet<>();
        Iterator<String> names = root.fieldNames();
        while (names.hasNext()) {
            fields.add(names.next());
        }
        if (!ALLOWED_FIELDS.containsAll(fields)) {
            throw new ContractException("Unknown database configuration fields");
        }
        String database = optionalText(root, "database");
        String schema = optionalText(root, "schema");
        if (database != null && schema != null && !database.equals(schema)) {
            throw new ContractException("Conflicting database names");
        }
        String effectiveDatabase = database == null ? schema : database;
        String host = root.has("host") ? requiredText(root, "host") : "localhost";
        String user = requiredText(root, "user");
        String password = root.has("password") ? requiredText(root, "password") : "";
        int port = root.has("port") ? requiredInt(root, "port") : 3306;
        String driverText = root.has("driver") ? requiredText(root, "driver") : DatabaseDriver.AUTO.value();
        if (effectiveDatabase == null) {
            throw new ContractException("Database name is required");
        }
        try {
            return new DatabaseConfiguration(host, effectiveDatabase, user, password, port, DatabaseDriver.parse(driverText));
        } catch (IllegalArgumentException error) {
            throw new ContractException("Invalid database configuration");
        }
    }

    public String host() {
        return host;
    }

    public String database() {
        return database;
    }

    public String schema() {
        return database;
    }

    public String user() {
        return user;
    }

    public String password() {
        return password;
    }

    public int port() {
        return port;
    }

    public DatabaseDriver driver() {
        return driver;
    }

    @Override
    public String toString() {
        return "DatabaseConfiguration{" +
                "host='" + host + '\'' +
                ", database='" + database + '\'' +
                ", user='" + user + '\'' +
                ", port=" + port +
                ", driver=" + driver.value() +
                '}';
    }

    private static String optionalText(JsonNode root, String name) {
        if (!root.has(name)) {
            return null;
        }
        return requiredText(root, name);
    }

    private static String requiredText(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || !value.isTextual()) {
            throw new ContractException("Expected text for database configuration field");
        }
        return value.textValue();
    }

    private static int requiredInt(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || !value.isIntegralNumber() || !value.canConvertToInt()) {
            throw new ContractException("Expected integer for database configuration field");
        }
        return value.intValue();
    }

    private static String requireText(String value, String name) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException("Invalid " + name);
        }
        return value;
    }
}
