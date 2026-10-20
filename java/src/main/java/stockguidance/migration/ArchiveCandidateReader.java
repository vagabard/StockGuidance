package stockguidance.migration;

import org.apache.fory.json.ForyJson;
import stockguidance.validation.ValidationContent;

import java.io.ByteArrayOutputStream;
import java.io.BufferedInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

public final class ArchiveCandidateReader {
    private static final BigDecimal MAX_PRICE = new BigDecimal("1e60");
    private static final BigDecimal MAX_LONG = BigDecimal.valueOf(Long.MAX_VALUE);
    private static final ZoneId EXCHANGE_ZONE = ZoneId.of("America/New_York");
    private static final ForyJson FORY_READER = ForyJson.builder().withFieldMode(true).build();

    private ArchiveCandidateReader() {
    }

    public static SessionArchive read(Path path, LocalDate session) {
        if (path == null || session == null) {
            throw new IllegalArgumentException("Archive path and session are required");
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
                rejectDuplicateKeys(line);
                ValidationContent.RawBar bar = decode(FORY_READER.fromJson(line, RawArchiveRecord.class), session);
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
        StringBuilder payload = new StringBuilder();
        payload.append("{\"records\":[");
        bars.values().stream().sorted(java.util.Comparator.comparing(ValidationContent.RawBar::ticker)).forEachOrdered(bar -> {
            if (payload.charAt(payload.length() - 1) != '[') payload.append(',');
            payload.append("{\"T\":");
            appendJsonString(bar.ticker(), payload);
            payload.append(",\"c\":");
            appendJsonString(canonicalDecimal(bar.close()), payload);
            payload.append(",\"h\":");
            appendJsonString(canonicalDecimal(bar.high()), payload);
            payload.append(",\"l\":");
            appendJsonString(canonicalDecimal(bar.low()), payload);
            payload.append(",\"n\":");
            if (bar.transactions() == null) payload.append("null"); else payload.append(bar.transactions());
            payload.append(",\"o\":");
            appendJsonString(canonicalDecimal(bar.open()), payload);
            payload.append(",\"v\":");
            appendJsonString(canonicalDecimal(bar.volume()), payload);
            payload.append(",\"vw\":");
            if (bar.vwap() == null) payload.append("null"); else appendJsonString(canonicalDecimal(bar.vwap()), payload);
            payload.append('}');
        });
        payload.append("],\"schema_version\":2}\n");
        try {
            return hex(MessageDigest.getInstance("SHA-256").digest(payload.toString().getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-256 is unavailable", error);
        }
    }

    private static void appendJsonString(String value, StringBuilder output) {
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
                        output.append("\\u");
                        output.append(String.format("%04x", (int) character));
                    } else {
                        output.append(character);
                    }
                }
            }
        }
        output.append('"');
    }

    public static String canonicalDecimal(BigDecimal value) {
        if (value == null) {
            throw new IllegalArgumentException("Decimal value is required");
        }
        return value.signum() == 0 ? "0" : value.stripTrailingZeros().toPlainString();
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

    private static BigDecimal price(BigDecimal value, String name) {
        value = quantity(value, name);
        if (value.signum() <= 0 || value.compareTo(MAX_PRICE) >= 0) {
            throw new IllegalArgumentException("Invalid archive price");
        }
        return value;
    }

    private static BigDecimal quantity(BigDecimal value, String name) {
        if (value == null) {
            throw new IllegalArgumentException("Invalid archive " + name);
        }
        return value;
    }

    private static void rejectDuplicateKeys(byte[] json) {
        int end = scanValue(json, skipWhitespace(json, 0));
        if (skipWhitespace(json, end) != json.length) {
            throw new IllegalArgumentException("Invalid archive JSON");
        }
    }

    private static int scanValue(byte[] json, int index) {
        if (index >= json.length) throw new IllegalArgumentException("Invalid archive JSON");
        return switch (json[index] & 0xff) {
            case '"' -> scanString(json, index, null).index();
            case '{' -> scanObject(json, index);
            case '[' -> scanArray(json, index);
            default -> scanPrimitive(json, index);
        };
    }

    private static int scanObject(byte[] json, int index) {
        Set<ByteKey> names = new HashSet<>();
        int cursor = skipWhitespace(json, index + 1);
        if (cursor < json.length && json[cursor] == '}') return cursor + 1;
        while (true) {
            if (cursor >= json.length || json[cursor] != '"') {
                throw new IllegalArgumentException("Invalid archive JSON");
            }
            ByteStringToken name = scanString(json, cursor, new ByteArrayOutputStream());
            if (!names.add(new ByteKey(name.value()))) throw new IllegalArgumentException("Duplicate JSON field");
            cursor = skipWhitespace(json, name.index());
            if (cursor >= json.length || json[cursor] != ':') {
                throw new IllegalArgumentException("Invalid archive JSON");
            }
            cursor = scanValue(json, skipWhitespace(json, cursor + 1));
            cursor = skipWhitespace(json, cursor);
            if (cursor >= json.length) throw new IllegalArgumentException("Invalid archive JSON");
            if (json[cursor] == '}') return cursor + 1;
            if (json[cursor] != ',') throw new IllegalArgumentException("Invalid archive JSON");
            cursor = skipWhitespace(json, cursor + 1);
        }
    }

    private static int scanArray(byte[] json, int index) {
        int cursor = skipWhitespace(json, index + 1);
        if (cursor < json.length && json[cursor] == ']') return cursor + 1;
        while (true) {
            cursor = scanValue(json, cursor);
            cursor = skipWhitespace(json, cursor);
            if (cursor >= json.length) throw new IllegalArgumentException("Invalid archive JSON");
            if (json[cursor] == ']') return cursor + 1;
            if (json[cursor] != ',') throw new IllegalArgumentException("Invalid archive JSON");
            cursor = skipWhitespace(json, cursor + 1);
        }
    }

    private static int scanPrimitive(byte[] json, int index) {
        int cursor = index;
        while (cursor < json.length && !isJsonWhitespace(json[cursor] & 0xff)
                && json[cursor] != ',' && json[cursor] != ']' && json[cursor] != '}') {
            if ((json[cursor] & 0xff) > 0x7f) throw new IllegalArgumentException("Invalid archive JSON");
            cursor++;
        }
        if (cursor == index) throw new IllegalArgumentException("Invalid archive JSON");
        return cursor;
    }

    private static ByteStringToken scanString(byte[] json, int index, ByteArrayOutputStream decodedKey) {
        int cursor = index + 1;
        while (cursor < json.length) {
            int character = json[cursor] & 0xff;
            if (character == '"') {
                return new ByteStringToken(decodedKey == null ? null : decodedKey.toByteArray(), cursor + 1);
            }
            if (character < 0x20) throw new IllegalArgumentException("Invalid archive JSON");
            if (character != '\\') {
                cursor = scanUtf8(json, cursor, decodedKey);
                continue;
            }
            cursor++;
            if (cursor >= json.length) throw new IllegalArgumentException("Invalid archive JSON");
            int escaped = json[cursor++] & 0xff;
            switch (escaped) {
                case '"', '\\', '/' -> appendCodeUnit(decodedKey, escaped);
                case 'b' -> appendCodeUnit(decodedKey, '\b');
                case 'f' -> appendCodeUnit(decodedKey, '\f');
                case 'n' -> appendCodeUnit(decodedKey, '\n');
                case 'r' -> appendCodeUnit(decodedKey, '\r');
                case 't' -> appendCodeUnit(decodedKey, '\t');
                case 'u' -> {
                    if (cursor + 4 > json.length) throw new IllegalArgumentException("Invalid archive JSON");
                    int code = 0;
                    for (int offset = 0; offset < 4; offset++) {
                        int digit = Character.digit(json[cursor + offset] & 0xff, 16);
                        if (digit < 0) throw new IllegalArgumentException("Invalid archive JSON");
                        code = (code << 4) | digit;
                    }
                    appendCodeUnit(decodedKey, code);
                    cursor += 4;
                }
                default -> throw new IllegalArgumentException("Invalid archive JSON");
            }
        }
        throw new IllegalArgumentException("Invalid archive JSON");
    }

    private static int scanUtf8(byte[] json, int index, ByteArrayOutputStream decodedKey) {
        int first = json[index] & 0xff;
        if (first < 0x80) {
            appendCodeUnit(decodedKey, first);
            return index + 1;
        }
        if (first >= 0xc2 && first <= 0xdf) {
            int second = continuation(json, index + 1);
            appendCodePoint(decodedKey, ((first & 0x1f) << 6) | (second & 0x3f));
            return index + 2;
        }
        if (first >= 0xe0 && first <= 0xef) {
            int second = continuation(json, index + 1);
            if (first == 0xe0 && second < 0xa0 || first == 0xed && second > 0x9f) {
                throw new IllegalArgumentException("Invalid archive UTF-8");
            }
            int third = continuation(json, index + 2);
            int codePoint = ((first & 0x0f) << 12) | ((second & 0x3f) << 6) | (third & 0x3f);
            appendCodePoint(decodedKey, codePoint);
            return index + 3;
        }
        if (first >= 0xf0 && first <= 0xf4) {
            int second = continuation(json, index + 1);
            if (first == 0xf0 && second < 0x90 || first == 0xf4 && second > 0x8f) {
                throw new IllegalArgumentException("Invalid archive UTF-8");
            }
            int third = continuation(json, index + 2);
            int fourth = continuation(json, index + 3);
            int codePoint = ((first & 0x07) << 18) | ((second & 0x3f) << 12)
                    | ((third & 0x3f) << 6) | (fourth & 0x3f);
            appendCodePoint(decodedKey, codePoint);
            return index + 4;
        }
        throw new IllegalArgumentException("Invalid archive UTF-8");
    }

    private static int continuation(byte[] json, int index) {
        if (index >= json.length) throw new IllegalArgumentException("Invalid archive UTF-8");
        int value = json[index] & 0xff;
        if ((value & 0xc0) != 0x80) throw new IllegalArgumentException("Invalid archive UTF-8");
        return value;
    }

    private static void appendCodePoint(ByteArrayOutputStream decodedKey, int codePoint) {
        if (codePoint <= 0xffff) {
            appendCodeUnit(decodedKey, codePoint);
            return;
        }
        int value = codePoint - 0x10000;
        appendCodeUnit(decodedKey, 0xd800 | (value >>> 10));
        appendCodeUnit(decodedKey, 0xdc00 | (value & 0x3ff));
    }

    private static void appendCodeUnit(ByteArrayOutputStream decodedKey, int codeUnit) {
        if (decodedKey == null) return;
        decodedKey.write(codeUnit >>> 8);
        decodedKey.write(codeUnit);
    }

    private static int skipWhitespace(byte[] json, int index) {
        int cursor = index;
        while (cursor < json.length && isJsonWhitespace(json[cursor] & 0xff)) cursor++;
        return cursor;
    }

    private static boolean isJsonWhitespace(int value) {
        return value == ' ' || value == '\t' || value == '\r' || value == '\n';
    }

    private record ByteStringToken(byte[] value, int index) {
    }

    private static final class ByteKey {
        private final byte[] value;
        private final int hash;

        private ByteKey(byte[] value) {
            this.value = value;
            this.hash = java.util.Arrays.hashCode(value);
        }

        @Override
        public boolean equals(Object other) {
            return other instanceof ByteKey key && java.util.Arrays.equals(value, key.value);
        }

        @Override
        public int hashCode() {
            return hash;
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
