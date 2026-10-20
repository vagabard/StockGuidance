package stockguidance.json;

import org.apache.fory.json.ForyJson;
import org.apache.fory.json.ForyJsonException;
import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
import org.apache.fory.json.codec.AbstractJsonValueCodec;
import org.apache.fory.json.reader.JsonReader;
import org.apache.fory.json.writer.JsonWriter;

import java.io.IOException;
import java.io.InputStream;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;

public final class JsonSupport {
    private static final ForyJson FORY = ForyJson.builder()
            .withFieldMode(true)
            .registerCodec(JsonObject.class, new StrictJsonObjectCodec())
            .build();

    private JsonSupport() {
    }

    public static JsonObject read(Path path) {
        if (path == null) throw new ContractException("An explicit JSON path is required");
        try (InputStream input = Files.newInputStream(path)) {
            return readObject(input);
        } catch (IOException | RuntimeException error) {
            if (error instanceof ContractException contractException) throw contractException;
            throw new ContractException("Invalid JSON document", error);
        }
    }

    public static JsonObject parse(String json) {
        if (json == null) throw new ContractException("JSON text is required");
        try {
            return FORY.fromJson(json, JsonObject.class);
        } catch (RuntimeException error) {
            if (error instanceof ContractException contractException) throw contractException;
            throw new ContractException("Invalid JSON document", error);
        }
    }

    public static JsonObject readObject(InputStream input) throws IOException {
        if (input == null) throw new ContractException("JSON input is required");
        try {
            return FORY.fromJson(input, JsonObject.class);
        } catch (RuntimeException error) {
            throw new ContractException("Invalid JSON document", error);
        }
    }

    public static JsonObject object() {
        return new JsonObject();
    }

    public static JsonArray array() {
        return new JsonArray();
    }

    public static JsonObject putObject(JsonObject parent, String name) {
        JsonObject child = object();
        parent.put(name, child);
        return child;
    }

    public static JsonArray putArray(JsonObject parent, String name) {
        JsonArray child = array();
        parent.put(name, child);
        return child;
    }

    public static JsonObject addObject(JsonArray parent) {
        JsonObject child = object();
        parent.add(child);
        return child;
    }

    public static JsonArray addArray(JsonArray parent) {
        JsonArray child = array();
        parent.add(child);
        return child;
    }

    public static Object deepCopy(Object value) {
        if (value instanceof JsonObject object) {
            JsonObject copy = object();
            object.forEach((name, item) -> copy.put(name, deepCopy(item)));
            return copy;
        }
        if (value instanceof JsonArray array) {
            JsonArray copy = array();
            array.forEach(item -> copy.add(deepCopy(item)));
            return copy;
        }
        return value;
    }

    public static boolean isObject(Object value) {
        return value instanceof JsonObject;
    }

    public static boolean isNull(Object value) {
        return value == null;
    }

    public static boolean isString(Object value) {
        return value instanceof String;
    }

    public static boolean isBoolean(Object value) {
        return value instanceof Boolean;
    }

    public static boolean isNumber(Object value) {
        return value instanceof Number;
    }

    public static boolean isIntegralNumber(Object value) {
        return value instanceof Byte || value instanceof Short || value instanceof Integer
                || value instanceof Long || value instanceof BigInteger;
    }

    public static String stringValue(Object value) {
        return value instanceof String string ? string : null;
    }

    public static String stringOrEmpty(Object value) {
        return value == null ? "" : String.valueOf(value);
    }

    public static boolean booleanOrDefault(Object value, boolean defaultValue) {
        return value instanceof Boolean booleanValue ? booleanValue : defaultValue;
    }

    public static int integerValue(Object value) {
        return ((Number) value).intValue();
    }

    public static int integerOrDefault(Object value) {
        return integerOrDefault(value, 0);
    }

    public static int integerOrDefault(Object value, int defaultValue) {
        return value instanceof Number number ? number.intValue() : defaultValue;
    }

    public static long longIntegerValue(Object value) {
        return ((Number) value).longValue();
    }

    public static BigDecimal decimalValue(Object value) {
        if (value instanceof BigDecimal decimal) return decimal;
        if (value instanceof BigInteger integer) return new BigDecimal(integer);
        if (value instanceof Number number) return BigDecimal.valueOf(number.doubleValue());
        throw new IllegalStateException("JSON value is not numeric");
    }

    public static boolean fitsInt(Object value) {
        if (!isIntegralNumber(value)) return false;
        BigInteger number = new BigInteger(String.valueOf(value));
        return number.compareTo(BigInteger.valueOf(Integer.MIN_VALUE)) >= 0
                && number.compareTo(BigInteger.valueOf(Integer.MAX_VALUE)) <= 0;
    }

    public static boolean fitsLong(Object value) {
        if (!isIntegralNumber(value)) return false;
        BigInteger number = new BigInteger(String.valueOf(value));
        return number.compareTo(BigInteger.valueOf(Long.MIN_VALUE)) >= 0
                && number.compareTo(BigInteger.valueOf(Long.MAX_VALUE)) <= 0;
    }

    public static JsonObject requireObject(Object value, String message) {
        if (!(value instanceof JsonObject object)) throw new IllegalArgumentException(message);
        return object;
    }

    public static JsonArray requireArray(Object value, String message) {
        if (!(value instanceof JsonArray array)) throw new IllegalArgumentException(message);
        return array;
    }

    private static final class StrictJsonObjectCodec extends AbstractJsonValueCodec<JsonObject> {
        @Override
        public JsonObject read(JsonReader reader) {
            if (reader.tryReadNull()) return null;
            return readObject(reader);
        }

        private Object readValue(JsonReader reader) {
            if (reader.tryReadNull()) return null;
            return switch (reader.peekToken()) {
                case '{' -> readObject(reader);
                case '[' -> readArray(reader);
                case '"' -> reader.readString();
                case 't', 'f' -> reader.readBoolean();
                default -> readNumber(reader);
            };
        }

        private JsonObject readObject(JsonReader reader) {
            reader.expect('{');
            JsonObject result = new JsonObject();
            if (reader.consume('}')) return result;
            while (true) {
                String name = reader.readFieldName();
                if (result.containsKey(name)) throw new ForyJsonException("Duplicate JSON field: " + name);
                reader.expect(':');
                result.put(name, readValue(reader));
                if (!reader.consumeCommaOrEndObject()) return result;
            }
        }

        private JsonArray readArray(JsonReader reader) {
            reader.expect('[');
            JsonArray result = new JsonArray();
            if (reader.consume(']')) return result;
            while (true) {
                result.add(readValue(reader));
                if (!reader.consumeCommaOrEndArray()) return result;
            }
        }

        private Number readNumber(JsonReader reader) {
            String text = reader.readNumberAsString();
            if (text.indexOf('.') >= 0 || text.indexOf('e') >= 0 || text.indexOf('E') >= 0) {
                return new BigDecimal(text);
            }
            try {
                return Long.valueOf(text);
            } catch (NumberFormatException ignored) {
                return new BigInteger(text);
            }
        }

        @Override
        public void write(JsonWriter writer, JsonObject value) {
            if (value == null) {
                writer.writeNull();
                return;
            }
            writeObject(writer, value);
        }

        private void writeValue(JsonWriter writer, Object value) {
            if (value == null) {
                writer.writeNull();
            } else if (value instanceof JsonObject object) {
                writeObject(writer, object);
            } else if (value instanceof JsonArray array) {
                writer.writeArrayStart();
                for (int index = 0; index < array.size(); index++) {
                    if (index > 0) writer.writeComma(index);
                    writeValue(writer, array.get(index));
                }
                writer.writeArrayEnd();
            } else if (value instanceof String text) {
                writer.writeString(text);
            } else if (value instanceof Boolean booleanValue) {
                writer.writeBoolean(booleanValue);
            } else if (value instanceof BigDecimal decimal) {
                writer.writeBigDecimal(decimal);
            } else if (value instanceof BigInteger integer) {
                writer.writeBigInteger(integer);
            } else if (value instanceof Float floatValue) {
                writer.writeFloat(floatValue);
            } else if (value instanceof Double doubleValue) {
                writer.writeDouble(doubleValue);
            } else if (value instanceof Number number) {
                writer.writeNumber(number.toString());
            } else {
                throw new ForyJsonException("Unsupported JSON value: " + value.getClass());
            }
        }

        private void writeObject(JsonWriter writer, JsonObject object) {
            writer.writeObjectStart();
            int index = 0;
            for (Map.Entry<String, Object> entry : object.entrySet()) {
                if (index > 0) writer.writeComma(index);
                writer.writeFieldName(entry.getKey());
                writeValue(writer, entry.getValue());
                index++;
            }
            writer.writeObjectEnd();
        }
    }
}
