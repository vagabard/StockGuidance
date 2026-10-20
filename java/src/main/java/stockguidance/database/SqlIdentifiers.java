package stockguidance.database;

import java.util.Set;
import java.util.regex.Pattern;

public final class SqlIdentifiers {
    private static final Pattern ALLOWED = Pattern.compile("[A-Za-z_][A-Za-z0-9_]*");

    private SqlIdentifiers() {
    }

    public static String require(String identifier, String name) {
        if (identifier == null || !ALLOWED.matcher(identifier).matches()) {
            throw new IllegalArgumentException("Invalid SQL identifier for " + name);
        }
        return identifier;
    }

    public static String requireAllowed(String identifier, String name, Set<String> allowed) {
        String validated = require(identifier, name);
        if (!allowed.contains(validated)) {
            throw new IllegalArgumentException("Unsupported SQL identifier for " + name);
        }
        return validated;
    }

    public static String quote(String identifier, String name) {
        return "`" + require(identifier, name) + "`";
    }

    public static String quote(String identifier) {
        return quote(identifier, "identifier");
    }
}
