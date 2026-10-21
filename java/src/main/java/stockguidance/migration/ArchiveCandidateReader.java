package stockguidance.migration;

import org.apache.fory.json.ForyJson;
import org.apache.fory.json.ForyJsonException;
import org.apache.fory.json.codec.AbstractJsonValueCodec;
import org.apache.fory.json.reader.JsonReader;
import org.apache.fory.json.writer.JsonWriter;
import stockguidance.validation.ValidationContent;

import java.io.ByteArrayOutputStream;
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
import java.util.*;

public final class ArchiveCandidateReader {
    private static final BigDecimal MAX_PRICE = new BigDecimal("1e60");
    private static final BigDecimal MAX_LONG = BigDecimal.valueOf(Long.MAX_VALUE);
    private static final ZoneId EXCHANGE_ZONE = ZoneId.of("America/New_York");
    private static final ForyJson FORY_READER = ForyJson.builder()
            .withFieldMode(true)
            .registerCodec(RawArchiveRecord.class, new RawArchiveRecordCodec())
            .build();

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
                ValidationContent.RawBar bar = decode(parse(line), session);
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

    private static final class RawArchiveRecordCodec extends AbstractJsonValueCodec<RawArchiveRecord> {
        @Override
        public RawArchiveRecord read(JsonReader reader) {
            if (reader.tryReadNull()) {
                return null;
            }
            reader.expect('{');
            RawArchiveRecord row = new RawArchiveRecord();
            Set<String> fields = new HashSet<>();
            if (reader.consume('}')) {
                return row;
            }
            while (true) {
                String name = reader.readFieldName();
                if (!fields.add(name)) {
                    throw new ForyJsonException("Duplicate JSON field: " + name);
                }
                reader.expect(':');
                switch (name) {
                    case "T" -> row.T = reader.readNullableString();
                    case "o" -> row.o = readDecimal(reader);
                    case "h" -> row.h = readDecimal(reader);
                    case "l" -> row.l = readDecimal(reader);
                    case "c" -> row.c = readDecimal(reader);
                    case "v" -> row.v = readDecimal(reader);
                    case "vw" -> row.vw = readDecimal(reader);
                    case "n" -> row.n = readLong(reader);
                    case "t" -> row.t = readLong(reader);
                    default -> reader.skipValue();
                }
                if (!reader.consumeCommaOrEndObject()) {
                    return row;
                }
            }
        }

        @Override
        public void write(JsonWriter writer, RawArchiveRecord row) {
            if (row == null) {
                writer.writeNull();
                return;
            }
            writer.writeObjectStart();
            writeField(writer, 0, "T", row.T);
            writeField(writer, 1, "o", row.o);
            writeField(writer, 2, "h", row.h);
            writeField(writer, 3, "l", row.l);
            writeField(writer, 4, "c", row.c);
            writeField(writer, 5, "v", row.v);
            writeField(writer, 6, "vw", row.vw);
            writeField(writer, 7, "n", row.n);
            writeField(writer, 8, "t", row.t);
            writer.writeObjectEnd();
        }

        private static BigDecimal readDecimal(JsonReader reader) {
            return reader.tryReadNull() ? null : reader.readBigDecimal();
        }

        private static Long readLong(JsonReader reader) {
            return reader.tryReadNull() ? null : reader.readLong();
        }

        private static void writeField(JsonWriter writer, int index, String name, Object value) {
            if (index > 0) {
                writer.writeComma(index);
            }
            writer.writeFieldName(name);
            if (value == null) {
                writer.writeNull();
            } else if (value instanceof String string) {
                writer.writeString(string);
            } else if (value instanceof BigDecimal decimal) {
                writer.writeBigDecimal(decimal);
            } else if (value instanceof Long number) {
                writer.writeLong(number);
            } else {
                throw new ForyJsonException("Unsupported archive JSON value: " + value.getClass());
            }
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

    private static RawArchiveRecord parse(byte[] line) {
        try {
            return FORY_READER.fromJson(line, RawArchiveRecord.class);
        } catch (ForyJsonException error) {
            throw new IllegalArgumentException("Invalid archive JSON", error);
        }
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
        private final byte[] buffer = new byte[64 * 1024];
        private final ByteArrayOutputStream partial = new ByteArrayOutputStream();
        private int position;
        private int limit;

        private LineReader(InputStream input, MessageDigest digest) {
            this.input = input;
            this.digest = digest;
        }

        private byte[] next() throws IOException {
            partial.reset();
            while (true) {
                if (position == limit) {
                    int count = input.read(buffer);
                    if (count < 0) return partial.size() == 0 ? null : partial.toByteArray();
                    digest.update(buffer, 0, count);
                    position = 0;
                    limit = count;
                }
                int start = position;
                while (position < limit && buffer[position] != '\n') position++;
                if (position < limit) {
                    byte[] line;
                    if (partial.size() == 0) {
                        int end = position;
                        if (end > start && buffer[end - 1] == '\r') end--;
                        line = Arrays.copyOfRange(buffer, start, end);
                    } else {
                        partial.write(buffer, start, position - start);
                        line = partial.toByteArray();
                        if (line.length > 0 && line[line.length - 1] == '\r') {
                            line = Arrays.copyOf(line, line.length - 1);
                        }
                    }
                    position++;
                    return line;
                }
                partial.write(buffer, start, position - start);
            }
        }
    }
}
