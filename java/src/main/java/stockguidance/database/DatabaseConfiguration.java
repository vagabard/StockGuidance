package stockguidance.database;

import stockguidance.json.ContractException;
import stockguidance.json.JsonSupport;
import org.apache.fory.json.JsonObject;

import java.nio.file.Path;
import java.util.HashSet;
import java.util.Iterator;
import java.util.Set;

public final class DatabaseConfiguration {
    private static final Set<String> ALLOWED_FIELDS = Set.of("host", "database", "schema", "user", "password", "port", "driver", "table_names_file");

    private final String host;
    private final String database;
    private final String user;
    private final String password;
    private final int port;
    private final DatabaseDriver driver;
    private final Path tableNamesFile;

    public DatabaseConfiguration(String host, String database, String user, String password, int port, DatabaseDriver driver) {
        this(host, database, user, password, port, driver, null);
    }

    public DatabaseConfiguration(String host, String database, String user, String password, int port, DatabaseDriver driver, Path tableNamesFile) {
        this.host = requireText(host, "host");
        this.database = requireText(database, "database");
        this.user = requireText(user, "user");
        this.password = password == null ? "" : password;
        if (port <= 0 || port > 65535) {
            throw new IllegalArgumentException("Invalid database port");
        }
        this.port = port;
        this.driver = driver == null ? DatabaseDriver.AUTO : driver;
        this.tableNamesFile = tableNamesFile == null ? null : tableNamesFile.toAbsolutePath().normalize();
    }

    public static DatabaseConfiguration load(Path path) {
        try {
            return decode(JsonSupport.read(path), path);
        } catch (ContractException | IllegalArgumentException error) {
            throw new IllegalArgumentException("Cannot read the explicit database JSON configuration");
        }
    }

    public static DatabaseConfiguration decode(JsonObject root) {
        return decode(root, null);
    }

    private static DatabaseConfiguration decode(JsonObject root, Path configurationPath) {
        if (root == null) {
            throw new ContractException("Database configuration must be a JSON object");
        }
        Set<String> fields = new HashSet<>();
        fields.addAll(root.keySet());
        if (!ALLOWED_FIELDS.containsAll(fields)) {
            throw new ContractException("Unknown database configuration fields");
        }
        String database = optionalText(root, "database");
        String schema = optionalText(root, "schema");
        if (database != null && schema != null && !database.equals(schema)) {
            throw new ContractException("Conflicting database names");
        }
        String effectiveDatabase = database == null ? schema : database;
        String host = root.containsKey("host") ? requiredText(root, "host") : "localhost";
        String user = requiredText(root, "user");
        String password = root.containsKey("password") ? requiredText(root, "password") : "";
        int port = root.containsKey("port") ? requiredInt(root, "port") : 3306;
        String driverText = root.containsKey("driver") ? requiredText(root, "driver") : DatabaseDriver.AUTO.value();
        Path tableNamesFile = configurationPath == null ? null : resolveTableNamesFile(configurationPath, root);
        if (effectiveDatabase == null) {
            throw new ContractException("Database name is required");
        }
        try {
            return new DatabaseConfiguration(host, effectiveDatabase, user, password, port, DatabaseDriver.parse(driverText), tableNamesFile);
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

    public Path tableNamesFile() {
        return tableNamesFile;
    }

    public TableNames loadTableNames() {
        return TableNames.load(tableNamesFile);
    }

    public TableNames loadTableNames(Path override) {
        return TableNames.load(override == null ? tableNamesFile : override);
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

    private static String optionalText(JsonObject root, String name) {
        if (!root.containsKey(name)) {
            return null;
        }
        return requiredText(root, name);
    }

    private static String requiredText(JsonObject root, String name) {
        Object value = root.get(name);
        if (!JsonSupport.isString(value)) {
            throw new ContractException("Expected text for database configuration field");
        }
        return JsonSupport.stringValue(value);
    }

    private static int requiredInt(JsonObject root, String name) {
        Object value = root.get(name);
        if (!JsonSupport.isIntegralNumber(value) || !JsonSupport.fitsInt(value)) {
            throw new ContractException("Expected integer for database configuration field");
        }
        return JsonSupport.integerValue(value);
    }

    private static Path resolveTableNamesFile(Path configurationPath, JsonObject root) {
        if (!root.containsKey("table_names_file") || JsonSupport.isNull(root.get("table_names_file"))) {
            return null;
        }
        String selected = requiredText(root, "table_names_file");
        if (selected.isBlank()) {
            throw new ContractException("Table-names file path must not be empty");
        }
        Path selectedPath = Path.of(selected);
        if (configurationPath == null) {
            return selectedPath.normalize();
        }
        if (selectedPath.isAbsolute()) {
            return selectedPath.normalize();
        }
        Path absoluteConfiguration = configurationPath.toAbsolutePath().normalize();
        return absoluteConfiguration.getParent().resolve(selectedPath).normalize();
    }

    private static String requireText(String value, String name) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException("Invalid " + name);
        }
        return value;
    }
}
