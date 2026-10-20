package stockguidance.json;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public final class CanonicalJson {
    private CanonicalJson() {
    }

    public static byte[] bytes(Object value) {
        if (value == null) throw new ContractException("Cannot hash a null JSON value");
        StringBuilder output = new StringBuilder();
        append(value, output);
        output.append('\n');
        return output.toString().getBytes(StandardCharsets.UTF_8);
    }

    public static String sha256(Object value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(bytes(value));
            StringBuilder result = new StringBuilder(digest.length * 2);
            for (byte item : digest) result.append(String.format("%02x", item & 0xff));
            return result.toString();
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-256 is unavailable", error);
        }
    }

    private static void append(Object value, StringBuilder output) {
        if (value instanceof JsonObject object) {
            output.append('{');
            List<String> names = new ArrayList<>(object.keySet());
            Collections.sort(names);
            for (int index = 0; index < names.size(); index++) {
                if (index > 0) output.append(',');
                String name = names.get(index);
                appendString(name, output);
                output.append(':');
                append(object.get(name), output);
            }
            output.append('}');
            return;
        }
        if (value instanceof JsonArray array) {
            output.append('[');
            for (int index = 0; index < array.size(); index++) {
                if (index > 0) output.append(',');
                append(array.get(index), output);
            }
            output.append(']');
            return;
        }
        if (value instanceof String text) {
            appendString(text, output);
            return;
        }
        if (value == null) {
            output.append("null");
            return;
        }
        if (value instanceof Boolean booleanValue) {
            output.append(booleanValue ? "true" : "false");
            return;
        }
        if (value instanceof Byte || value instanceof Short || value instanceof Integer
                || value instanceof Long || value instanceof BigInteger) {
            output.append(new BigInteger(String.valueOf(value)));
            return;
        }
        if (value instanceof Float || value instanceof Double || value instanceof BigDecimal) {
            BigDecimal number = JsonSupport.decimalValue(value).stripTrailingZeros();
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
                    if (character < 0x20 || character > 0x7e) output.append(String.format("\\u%04x", (int) character));
                    else output.append(character);
                }
            }
        }
        output.append('"');
    }
}
