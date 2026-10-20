package stockguidance.migration;

import com.fasterxml.jackson.core.JsonFactory;
import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.core.StreamReadFeature;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.apache.fory.json.ForyJson;
import stockguidance.json.CanonicalJson;
import stockguidance.validation.ValidationContent;

import java.io.ByteArrayOutputStream;
import java.io.BufferedInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.math.BigDecimal;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.Map;
import java.util.TreeMap;

public final class ArchiveCandidateReader {
    private static final BigDecimal MAX_PRICE = new BigDecimal("1e60");
    private static final BigDecimal MAX_LONG = BigDecimal.valueOf(Long.MAX_VALUE);
    private static final ZoneId EXCHANGE_ZONE = ZoneId.of("America/New_York");
    private static final ObjectMapper READER = new ObjectMapper(JsonFactory.builder()
            .enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION).build())
            .enable(DeserializationFeature.USE_BIG_DECIMAL_FOR_FLOATS)
            .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
            .setNodeFactory(JsonNodeFactory.withExactBigDecimals(true));
    private static final JsonFactory STRICT_VALIDATOR = JsonFactory.builder()
            .enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
            .build();
    private static final ForyJson FORY_READER = ForyJson.builder().withFieldMode(true).build();

    private ArchiveCandidateReader() {
    }

    public static SessionArchive read(Path path, LocalDate session) {
        return read(path, session, Parser.JACKSON);
    }

    public static SessionArchive read(Path path, LocalDate session, Parser parser) {
        if (path == null || session == null) {
            throw new IllegalArgumentException("Archive path and session are required");
        }
        if (parser == null) {
            throw new IllegalArgumentException("Archive parser is required");
        }
        MessageDigest digest = sha256();
        Map<String, ValidationContent.RawBar> bars = new TreeMap<>();
        try (InputStream input = new BufferedInputStream(Files.newInputStream(path))) {
            LineReader lines = new LineReader(input, digest);
            byte[] line;
            while ((line = lines.next()) != null) {
                if (line.length == 0) {
                    throw new IllegalArgumentException("Archive record is empty");
                }
                ValidationContent.RawBar bar;
                if (parser == Parser.JACKSON) {
                    bar = decode(READER.readTree(decodeUtf8(line)), session);
                } else {
                    validateStrictJson(line);
                    bar = decode(FORY_READER.fromJson(line, RawArchiveRecord.class), session);
                }
                if (bars.putIfAbsent(bar.ticker(), bar) != null) {
                    throw new IllegalArgumentException("Duplicate archive ticker");
                }
            }
        } catch (IOException error) {
            throw new IllegalArgumentException("Cannot read archive file", error);
        }
        if (bars.isEmpty()) {
            throw new IllegalArgumentException("Empty archive does not establish no trading");
        }
        String fileSha256 = hex(digest.digest());
        return new SessionArchive(path, session, fileSha256, Map.copyOf(bars), recordsId(bars));
    }

    public enum Parser {
        JACKSON,
        FORY;

        public static Parser parse(String value) {
            if (value == null) throw new IllegalArgumentException("Archive parser is required");
            return switch (value.toLowerCase(java.util.Locale.ROOT)) {
                case "jackson" -> JACKSON;
                case "fory" -> FORY;
                default -> throw new IllegalArgumentException("Unknown archive parser: " + value);
            };
        }
    }

    public static final class RawArchiveRecord {
        public String T;
        public BigDecimal o;
        public BigDecimal h;
        public BigDecimal l;
        public BigDecimal c;
        public BigDecimal v;
        public BigDecimal vw;
        public Long n;
        public Long t;

        public RawArchiveRecord() {
        }
    }

    public static String recordsId(Map<String, ValidationContent.RawBar> bars) {
        ObjectNode payload = JsonNodeFactory.instance.objectNode();
        payload.put("schema_version", 2);
        ArrayNode records = payload.putArray("records");
        bars.values().stream().sorted(java.util.Comparator.comparing(ValidationContent.RawBar::ticker)).forEach(bar -> {
            ObjectNode row = records.addObject();
            row.put("T", bar.ticker());
            row.put("o", canonicalDecimal(bar.open()));
            row.put("h", canonicalDecimal(bar.high()));
            row.put("l", canonicalDecimal(bar.low()));
            row.put("c", canonicalDecimal(bar.close()));
            row.put("v", canonicalDecimal(bar.volume()));
            if (bar.vwap() == null) row.putNull("vw"); else row.put("vw", canonicalDecimal(bar.vwap()));
            if (bar.transactions() == null) row.putNull("n"); else row.put("n", bar.transactions());
        });
        return CanonicalJson.sha256(payload);
    }

    public static String canonicalDecimal(BigDecimal value) {
        if (value == null) {
            throw new IllegalArgumentException("Decimal value is required");
        }
        return value.signum() == 0 ? "0" : value.stripTrailingZeros().toPlainString();
    }

    private static ValidationContent.RawBar decode(JsonNode row, LocalDate session) {
        if (row == null || !row.isObject()) {
            throw new IllegalArgumentException("Archive record must be an object");
        }
        String ticker = text(row, "T");
        if (ticker.isEmpty() || ticker.length() > 16 || ticker.codePoints().anyMatch(Character::isWhitespace)) {
            throw new IllegalArgumentException("Invalid archive ticker");
        }
        JsonNode timestamp = row.get("t");
        if (timestamp == null || !timestamp.isIntegralNumber() || !timestamp.canConvertToLong() || timestamp.longValue() < 0) {
            throw new IllegalArgumentException("Invalid archive timestamp");
        }
        if (!Instant.ofEpochMilli(timestamp.longValue()).atZone(EXCHANGE_ZONE).toLocalDate().equals(session)) {
            throw new IllegalArgumentException("Archive timestamp and session disagree");
        }
        BigDecimal open = price(row, "o");
        BigDecimal high = price(row, "h");
        BigDecimal low = price(row, "l");
        BigDecimal close = price(row, "c");
        BigDecimal volume = quantity(row.get("v"), "volume");
        if (volume.signum() < 0 || volume.compareTo(MAX_LONG) > 0) {
            throw new IllegalArgumentException("Invalid archive volume");
        }
        BigDecimal vwap = row.get("vw") == null || row.get("vw").isNull() ? null : price(row, "vw");
        Long transactions = integer(row.get("n"), "transactions");
        if (low.compareTo(open.min(close)) > 0 || high.compareTo(open.max(close)) < 0 || low.compareTo(high) > 0) {
            throw new IllegalArgumentException("Inconsistent archive OHLC");
        }
        return new ValidationContent.RawBar(ticker, open, high, low, close, volume, vwap, transactions);
    }

    private static ValidationContent.RawBar decode(RawArchiveRecord row, LocalDate session) {
        if (row == null) {
            throw new IllegalArgumentException("Archive record must be an object");
        }
        String ticker = row.T;
        if (ticker == null || ticker.isEmpty() || ticker.length() > 16 || ticker.codePoints().anyMatch(Character::isWhitespace)) {
            throw new IllegalArgumentException("Invalid archive ticker");
        }
        if (row.t == null || row.t < 0) {
            throw new IllegalArgumentException("Invalid archive timestamp");
        }
        if (!Instant.ofEpochMilli(row.t).atZone(EXCHANGE_ZONE).toLocalDate().equals(session)) {
            throw new IllegalArgumentException("Archive timestamp and session disagree");
        }
        BigDecimal open = price(row.o, "o");
        BigDecimal high = price(row.h, "h");
        BigDecimal low = price(row.l, "l");
        BigDecimal close = price(row.c, "c");
        BigDecimal volume = quantity(row.v, "volume");
        if (volume.signum() < 0 || volume.compareTo(MAX_LONG) > 0) {
            throw new IllegalArgumentException("Invalid archive volume");
        }
        BigDecimal vwap = row.vw == null ? null : price(row.vw, "vw");
        Long transactions = row.n;
        if (transactions != null && transactions < 0) {
            throw new IllegalArgumentException("Invalid transactions");
        }
        if (low.compareTo(open.min(close)) > 0 || high.compareTo(open.max(close)) < 0 || low.compareTo(high) > 0) {
            throw new IllegalArgumentException("Inconsistent archive OHLC");
        }
        return new ValidationContent.RawBar(ticker, open, high, low, close, volume, vwap, transactions);
    }

    private static BigDecimal price(JsonNode row, String name) {
        BigDecimal value = quantity(row.get(name), name);
        if (value.signum() <= 0 || value.compareTo(MAX_PRICE) >= 0) {
            throw new IllegalArgumentException("Invalid archive price");
        }
        return value;
    }

    private static BigDecimal price(BigDecimal value, String name) {
        value = quantity(value, name);
        if (value.signum() <= 0 || value.compareTo(MAX_PRICE) >= 0) {
            throw new IllegalArgumentException("Invalid archive price");
        }
        return value;
    }

    private static BigDecimal quantity(JsonNode value, String name) {
        if (value == null || (!value.isNumber() && !value.isTextual())) {
            throw new IllegalArgumentException("Invalid archive " + name);
        }
        try {
            return new BigDecimal(value.asText());
        } catch (NumberFormatException error) {
            throw new IllegalArgumentException("Invalid archive " + name, error);
        }
    }

    private static BigDecimal quantity(BigDecimal value, String name) {
        if (value == null) {
            throw new IllegalArgumentException("Invalid archive " + name);
        }
        return value;
    }

    private static void validateStrictJson(byte[] bytes) {
        try (JsonParser parser = STRICT_VALIDATOR.createParser(bytes)) {
            while (parser.nextToken() != null) {
            }
        } catch (IOException error) {
            throw new IllegalArgumentException("Invalid archive JSON", error);
        }
    }

    private static Long integer(JsonNode value, String name) {
        if (value == null || value.isNull()) return null;
        if (!value.isIntegralNumber() || !value.canConvertToLong() || value.longValue() < 0) {
            throw new IllegalArgumentException("Invalid " + name);
        }
        return value.longValue();
    }

    private static String text(JsonNode row, String name) {
        JsonNode value = row.get(name);
        if (value == null || !value.isTextual()) {
            throw new IllegalArgumentException("Invalid archive ticker");
        }
        return value.textValue();
    }

    private static String decodeUtf8(byte[] bytes) {
        try {
            return StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(bytes)).toString();
        } catch (CharacterCodingException error) {
            throw new IllegalArgumentException("Archive is not valid UTF-8", error);
        }
    }

    private static MessageDigest sha256() {
        try {
            return MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-256 is unavailable", error);
        }
    }

    private static String hex(byte[] bytes) {
        return java.util.HexFormat.of().formatHex(bytes);
    }

    public record SessionArchive(Path path, LocalDate session, String fileSha256,
                                 Map<String, ValidationContent.RawBar> bars, String recordsId) {
    }

    private static final class LineReader {
        private final InputStream input;
        private final MessageDigest digest;

        private LineReader(InputStream input, MessageDigest digest) {
            this.input = input;
            this.digest = digest;
        }

        private byte[] next() throws IOException {
            ByteArrayOutputStream line = new ByteArrayOutputStream();
            boolean read = false;
            int value;
            while ((value = input.read()) >= 0) {
                read = true;
                digest.update((byte) value);
                if (value == '\n') {
                    byte[] bytes = line.toByteArray();
                    if (bytes.length > 0 && bytes[bytes.length - 1] == '\r') {
                        return java.util.Arrays.copyOf(bytes, bytes.length - 1);
                    }
                    return bytes;
                }
                line.write(value);
            }
            return read ? line.toByteArray() : null;
        }
    }
}
