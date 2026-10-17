package stockguidance.validation;

import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.MySqlConnectionFactory;
import stockguidance.json.CanonicalJson;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;

public final class ValidatorMain {
    private ValidatorMain() {
    }

    public static void main(String[] arguments) {
        try {
            Map<String, Path> options = options(arguments);
            DatabaseConfiguration configuration = DatabaseConfiguration.load(options.get("config"));
            ValidationRequest request = ValidationRequest.read(options.get("request"));
            ObjectNode report = new StreamingValidator(new MySqlConnectionFactory()).validate(configuration, request);
            String output = new String(CanonicalJson.bytes(report), StandardCharsets.UTF_8);
            if (options.containsKey("output")) {
                Files.writeString(options.get("output"), output, StandardCharsets.UTF_8);
            }
            System.out.print(output);
            System.exit(report.get("exit_code").intValue());
        } catch (Exception error) {
            ObjectNode failure = JsonNodeFactory.instance.objectNode();
            failure.put("schema_version", 1);
            failure.put("backend", "java");
            failure.put("complete", false);
            failure.put("failed", true);
            failure.put("exit_code", 1);
            failure.put("error", "Java validation backend failed");
            System.out.print(new String(CanonicalJson.bytes(failure), StandardCharsets.UTF_8));
            System.exit(1);
        }
    }

    private static Map<String, Path> options(String[] arguments) {
        if (arguments == null) {
            throw new IllegalArgumentException("Validator arguments are required");
        }
        Map<String, Path> result = new HashMap<>();
        for (int index = 0; index < arguments.length; index += 2) {
            if (index + 1 >= arguments.length || !(arguments[index].equals("--config") || arguments[index].equals("--request") || arguments[index].equals("--output"))) {
                throw new IllegalArgumentException("Validator requires explicit config and request paths");
            }
            String name = arguments[index].substring(2);
            if (result.put(name, Path.of(arguments[index + 1])) != null) {
                throw new IllegalArgumentException("Validator option was repeated");
            }
        }
        if (!result.containsKey("config") || !result.containsKey("request")) {
            throw new IllegalArgumentException("Validator requires explicit config and request paths");
        }
        return result;
    }
}
