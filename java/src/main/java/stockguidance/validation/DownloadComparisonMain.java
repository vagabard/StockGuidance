package stockguidance.validation;

import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.MySqlConnectionFactory;
import stockguidance.database.TableNames;

import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.HashMap;
import java.util.Map;

public final class DownloadComparisonMain {
    private DownloadComparisonMain() {
    }

    public static void main(String[] arguments) {
        try {
            Map<String, String> options = options(arguments);
            DatabaseConfiguration configuration = DatabaseConfiguration.load(Path.of(required(options, "config")));
            TableNames tableNames = configuration.loadTableNames(options.containsKey("table-names-file") ? Path.of(options.get("table-names-file")) : null);
            Path dataRoot = Path.of(required(options, "data-root"));
            String adjustment = options.getOrDefault("adjustment", "adjusted");
            LocalDate start = optionalDate(options.get("start-date"));
            LocalDate end = optionalDate(options.get("end-date"));
            if (start != null && end != null && start.isAfter(end)) {
                throw new IllegalArgumentException("Comparison start follows end");
            }
            var result = new LegacyDownloadComparator(new MySqlConnectionFactory(), tableNames).compare(configuration, dataRoot, start, end, adjustment);
            String output = new String(stockguidance.json.CanonicalJson.bytes(result.report()), StandardCharsets.UTF_8);
            System.out.print(output);
            System.exit(result.report().get("exit_code").intValue());
        } catch (Exception error) {
            var failure = com.fasterxml.jackson.databind.node.JsonNodeFactory.instance.objectNode();
            failure.put("schema_version", 1);
            failure.put("backend", "java");
            failure.put("operation", "compare_database_downloads");
            failure.put("complete", false);
            failure.put("failed", true);
            failure.put("exit_code", 1);
            failure.put("error", "Legacy download comparison failed");
            System.out.print(new String(stockguidance.json.CanonicalJson.bytes(failure), StandardCharsets.UTF_8));
            System.exit(1);
        }
    }

    private static Map<String, String> options(String[] arguments) {
        if (arguments == null) {
            throw new IllegalArgumentException("Comparison arguments are required");
        }
        Map<String, String> result = new HashMap<>();
        for (int index = 0; index < arguments.length; index += 2) {
            if (index + 1 >= arguments.length) {
                throw new IllegalArgumentException("Comparison options require values");
            }
            String option = arguments[index];
            if (!(option.equals("--config") || option.equals("--data-root") || option.equals("--start-date") || option.equals("--end-date") || option.equals("--adjustment") || option.equals("--table-names-file"))) {
                throw new IllegalArgumentException("Unsupported comparison option");
            }
            if (result.put(option.substring(2), arguments[index + 1]) != null) {
                throw new IllegalArgumentException("Comparison option was repeated");
            }
        }
        if (!result.containsKey("config") || !result.containsKey("data-root")) {
            throw new IllegalArgumentException("Comparison requires --config and --data-root");
        }
        return result;
    }

    private static String required(Map<String, String> options, String name) {
        String value = options.get(name);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("Comparison requires an explicit " + name);
        }
        return value;
    }

    private static LocalDate optionalDate(String value) {
        if (value == null) {
            return null;
        }
        try {
            return LocalDate.parse(value);
        } catch (DateTimeParseException error) {
            throw new IllegalArgumentException("Comparison date is invalid");
        }
    }
}
