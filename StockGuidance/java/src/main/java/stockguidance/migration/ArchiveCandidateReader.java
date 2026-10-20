package stockguidance.migration;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import stockguidance.json.CanonicalJson;
import stockguidance.json.JsonSupport;
import stockguidance.validation.ValidationContent;

import java.io.ByteArrayOutputStream;
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

    private ArchiveCandidateReader() {
    }

    public static SessionArchive read(Path path, LocalDate session) {
        if (path == null || session == null) {
            throw new IllegalArgumentException("Archive path and session are required");
        }
        MessageDigest digest = sha256();
        Map<String, ValidationContent.RawBar> bars = new TreeMap<>();
        try (InputStream input = Files.newInputStream(path)) {
            LineReader lines = new LineReader(input, digest);
            byte[] line;
            while ((line = lines.next()) != null) {
                if (line.length == 0) {
                    throw new IllegalArgumentException("Archive record is empty");
                }
                JsonNode row = JsonSupport.parse(decodeUtf8(line));
                if (!row.isObject()) {
                    throw new IllegalArgumentException("Archive record must be an object");
                }
                ValidationContent.RawBar bar = decode(row, session);
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

    private static BigDecimal price(JsonNode row, String name) {
        BigDecimal value = quantity(row.get(name), name);
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
