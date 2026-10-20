package stockguidance.database;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
import stockguidance.json.ContractException;
import stockguidance.json.JsonSupport;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.EnumMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

public final class TableNames {
    private static final String RESOURCE = "/stockguidance/database/table_names.json";
    private static final Pattern IDENTIFIER = Pattern.compile("[A-Za-z_][A-Za-z0-9_]*");
    private static final String WINDOW = "{window}";

    public enum Role {
        SOURCE("source"),
        SESSIONS("sessions"),
        TICKERS("tickers"),
        PRICES("prices"),
        DELTAS("deltas"),
        IMPORTS("imports"),
        ZSCORE_SERIES("zscore_series"),
        ZSCORE_SESSIONS("zscore_sessions");

        private final String key;

        Role(String key) {
            this.key = key;
        }

        public String key() {
            return key;
        }
    }

    public record ResolvedZScoreTables(String price, String priceDelta) {
        public ResolvedZScoreTables {
            validateIdentifier(price, "price z-score table name");
            validateIdentifier(priceDelta, "price_delta z-score table name");
        }
    }

    private final EnumMap<Role, String> tables;
    private final String priceTemplate;
    private final String priceDeltaTemplate;

    private TableNames(EnumMap<Role, String> tables, String priceTemplate, String priceDeltaTemplate) {
        this.tables = new EnumMap<>(tables);
        this.priceTemplate = validateTemplate(priceTemplate, "price");
        this.priceDeltaTemplate = validateTemplate(priceDeltaTemplate, "price_delta");
        Set<String> names = new HashSet<>();
        for (String name : this.tables.values()) {
            if (!names.add(fold(name))) {
                throw new ContractException("Table names must be unique case-insensitively");
            }
        }
    }

    public static TableNames load(Path path) {
        return path == null ? loadPackaged() : decode(JsonSupport.read(path));
    }

    public static TableNames loadPackaged() {
        try (InputStream input = TableNames.class.getResourceAsStream(RESOURCE)) {
            if (input == null) {
                throw new ContractException("The packaged table-names resource is missing");
            }
            return decode(JsonSupport.readObject(input));
        } catch (IOException | RuntimeException error) {
            if (error instanceof ContractException contractException) {
                throw contractException;
            }
            throw new ContractException("Cannot read the packaged table-names resource");
        }
    }

    public static TableNames decode(JsonObject root) {
        requireObject(root, "Table-names JSON");
        requireFields(root, Set.of("schema_version", "tables", "zscore_templates"));
        Object version = root.get("schema_version");
        if (!JsonSupport.isIntegralNumber(version) || !JsonSupport.fitsInt(version) || JsonSupport.integerValue(version) != 1) {
            throw new ContractException("Unsupported table-names schema version");
        }
        JsonObject tableValues = object(root, "tables");
        Set<String> tableFields = new HashSet<>();
        for (Role role : Role.values()) {
            tableFields.add(role.key());
        }
        requireFields(tableValues, tableFields);
        EnumMap<Role, String> tables = new EnumMap<>(Role.class);
        for (Role role : Role.values()) {
            tables.put(role, validateIdentifier(text(tableValues, role.key()), role.key() + " table name"));
        }
        JsonObject templateValues = object(root, "zscore_templates");
        requireFields(templateValues, Set.of("price", "price_delta"));
        return new TableNames(tables, text(templateValues, "price"), text(templateValues, "price_delta"));
    }

    public String table(Role role) {
        if (role == null) {
            throw new IllegalArgumentException("Unknown table role");
        }
        return tables.get(role);
    }

    public String source() {
        return table(Role.SOURCE);
    }

    public String sessions() {
        return table(Role.SESSIONS);
    }

    public String tickers() {
        return table(Role.TICKERS);
    }

    public String prices() {
        return table(Role.PRICES);
    }

    public String deltas() {
        return table(Role.DELTAS);
    }

    public String imports() {
        return table(Role.IMPORTS);
    }

    public String zscoreSeries() {
        return table(Role.ZSCORE_SERIES);
    }

    public String zscoreSessions() {
        return table(Role.ZSCORE_SESSIONS);
    }

    public String priceTemplate() {
        return priceTemplate;
    }

    public String priceDeltaTemplate() {
        return priceDeltaTemplate;
    }

    public Map<String, String> tables() {
        Map<String, String> result = new LinkedHashMap<>();
        for (Role role : Role.values()) {
            result.put(role.key(), table(role));
        }
        return Map.copyOf(result);
    }

    public Map<String, String> templates() {
        return Map.of("price", priceTemplate, "price_delta", priceDeltaTemplate);
    }

    public Map<Integer, ResolvedZScoreTables> resolveZScoreTables(Iterable<?> windows) {
        if (windows == null) {
            throw new IllegalArgumentException("Z-score windows are required");
        }
        List<Integer> values = new ArrayList<>();
        Set<Integer> seenWindows = new HashSet<>();
        for (Object value : windows) {
            if (!(value instanceof Integer window) || window < 2 || !seenWindows.add(window)) {
                throw new IllegalArgumentException("Z-score windows must be distinct integers of at least 2");
            }
            values.add(window);
        }
        Set<String> used = new HashSet<>();
        for (String name : tables.values()) {
            used.add(fold(name));
        }
        Map<Integer, ResolvedZScoreTables> result = new LinkedHashMap<>();
        for (Integer window : values) {
            String price = validateIdentifier(priceTemplate.replace(WINDOW, window.toString()), "price z-score table name");
            String priceDelta = validateIdentifier(priceDeltaTemplate.replace(WINDOW, window.toString()), "price_delta z-score table name");
            if (!used.add(fold(price)) || !used.add(fold(priceDelta))) {
                throw new IllegalArgumentException("Z-score table names collide with an existing table");
            }
            result.put(window, new ResolvedZScoreTables(price, priceDelta));
        }
        return Map.copyOf(result);
    }

    public static String validateIdentifier(String value, String name) {
        if (value == null || value.isEmpty() || value.length() > 64 || !IDENTIFIER.matcher(value).matches()) {
            throw new IllegalArgumentException("Invalid " + name);
        }
        return value;
    }

    private static String validateTemplate(String value, String name) {
        if (value == null || value.isEmpty() || count(value, WINDOW) != 1) {
            throw new IllegalArgumentException("Invalid " + name + " z-score template");
        }
        String remainder = value.replace(WINDOW, "");
        if (remainder.indexOf('{') >= 0 || remainder.indexOf('}') >= 0) {
            throw new IllegalArgumentException("Invalid " + name + " z-score template");
        }
        return value;
    }

    private static int count(String value, String target) {
        int count = 0;
        int offset = 0;
        while ((offset = value.indexOf(target, offset)) >= 0) {
            count++;
            offset += target.length();
        }
        return count;
    }

    private static String fold(String value) {
        return value.toLowerCase(Locale.ROOT);
    }

    private static JsonObject object(JsonObject root, String name) {
        Object value = root.get(name);
        requireObject(value, name);
        return (JsonObject) value;
    }

    private static String text(JsonObject root, String name) {
        Object value = root.get(name);
        if (!JsonSupport.isString(value) || JsonSupport.stringValue(value).isEmpty()) {
            throw new ContractException("Expected text for table-names field");
        }
        return JsonSupport.stringValue(value);
    }

    private static void requireObject(Object value, String name) {
        if (!JsonSupport.isObject(value)) {
            throw new ContractException(name + " must be an object");
        }
    }

    private static void requireFields(JsonObject value, Set<String> expected) {
        Set<String> actual = new HashSet<>(value.keySet());
        if (!actual.equals(expected)) {
            throw new ContractException("Incomplete or unsupported table-names fields");
        }
    }
}
