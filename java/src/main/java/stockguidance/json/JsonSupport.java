package stockguidance.json;

import com.fasterxml.jackson.core.JsonFactory;
import com.fasterxml.jackson.core.StreamReadFeature;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

public final class JsonSupport {
    private static final ObjectMapper MAPPER = createMapper();

    private JsonSupport() {
    }

    public static JsonNode read(Path path) {
        if (path == null) {
            throw new ContractException("An explicit JSON path is required");
        }
        try (InputStream input = Files.newInputStream(path)) {
            return parseTree(input);
        } catch (IOException | RuntimeException error) {
            if (error instanceof ContractException contractException) {
                throw contractException;
            }
            throw new ContractException("Invalid JSON document");
        }
    }

    public static JsonNode parse(String json) {
        if (json == null) {
            throw new ContractException("JSON text is required");
        }
        try {
            return parseTree(json);
        } catch (IOException | RuntimeException error) {
            if (error instanceof ContractException contractException) {
                throw contractException;
            }
            throw new ContractException("Invalid JSON document");
        }
    }

    public static JsonNode readTree(InputStream input) throws IOException {
        if (input == null) {
            throw new ContractException("JSON input is required");
        }
        JsonNode result = parseTree(input);
        return result;
    }

    public static byte[] utf8(String json) {
        return json.getBytes(StandardCharsets.UTF_8);
    }

    private static JsonNode parseTree(InputStream input) throws IOException {
        JsonNode result = MAPPER.readTree(input);
        if (result == null) {
            throw new ContractException("JSON document must contain one value");
        }
        return result;
    }

    private static JsonNode parseTree(String json) throws IOException {
        JsonNode result = MAPPER.readTree(json);
        if (result == null) {
            throw new ContractException("JSON document must contain one value");
        }
        return result;
    }

    private static ObjectMapper createMapper() {
        JsonFactory factory = JsonFactory.builder()
                .enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
                .build();
        ObjectMapper mapper = new ObjectMapper(factory);
        mapper.enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS);
        mapper.setNodeFactory(JsonNodeFactory.withExactBigDecimals(true));
        return mapper;
    }
}
