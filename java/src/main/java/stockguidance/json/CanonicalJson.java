package stockguidance.json;

import com.fasterxml.jackson.databind.JsonNode;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Iterator;
import java.util.List;
import java.util.Map;

public final class CanonicalJson {
    private CanonicalJson() {
    }

    public static byte[] bytes(JsonNode value) {
        if (value == null) {
            throw new ContractException("Cannot hash a null JSON node");
        }
        StringBuilder output = new StringBuilder();
        append(value, output);
        output.append('\n');
        return output.toString().getBytes(StandardCharsets.UTF_8);
    }

    public static String sha256(JsonNode value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(bytes(value));
            StringBuilder result = new StringBuilder(digest.length * 2);
            for (byte item : digest) {
                result.append(String.format("%02x", item & 0xff));
            }
            return result.toString();
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-256 is unavailable", error);
        }
    }

    private static void append(JsonNode value, StringBuilder output) {
        if (value.isObject()) {
            output.append('{');
            List<String> names = new ArrayList<>();
            value.fieldNames().forEachRemaining(names::add);
            Collections.sort(names);
            for (int index = 0; index < names.size(); index++) {
                if (index > 0) {
                    output.append(',');
                }
                String name = names.get(index);
                appendString(name, output);
                output.append(':');
                append(value.get(name), output);
            }
            output.append('}');
            return;
        }
        if (value.isArray()) {
            output.append('[');
            Iterator<JsonNode> values = value.elements();
            boolean first = true;
            while (values.hasNext()) {
                if (!first) {
                    output.append(',');
                }
                append(values.next(), output);
                first = false;
            }
            output.append(']');
            return;
        }
        if (value.isTextual()) {
            appendString(value.textValue(), output);
            return;
        }
        if (value.isNull()) {
            output.append("null");
            return;
        }
        if (value.isBoolean()) {
            output.append(value.booleanValue() ? "true" : "false");
            return;
        }
        if (value.isIntegralNumber()) {
            output.append(new BigInteger(value.asText()).toString());
            return;
        }
        if (value.isFloatingPointNumber()) {
            BigDecimal number = value.decimalValue().stripTrailingZeros();
            output.append(number.signum() == 0 ? "0" : number.toPlainString());
            return;
        }
        throw new ContractException("Unsupported JSON value for canonical encoding");
    }

    private static void appendString(String value, StringBuilder output) {
        output.append('"');
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            switch (character) {
                case '"' -> output.append("\\\"");
                case '\\' -> output.append("\\\\");
                case '\b' -> output.append("\\b");
                case '\f' -> output.append("\\f");
                case '\n' -> output.append("\\n");
                case '\r' -> output.append("\\r");
                case '\t' -> output.append("\\t");
                default -> {
                    if (character < 0x20 || character > 0x7e) {
                        output.append(String.format("\\u%04x", (int) character));
                    } else {
                        output.append(character);
                    }
                }
            }
        }
        output.append('"');
    }
}
