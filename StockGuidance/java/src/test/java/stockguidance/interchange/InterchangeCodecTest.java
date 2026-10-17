package stockguidance.interchange;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;
import stockguidance.json.CanonicalJson;
import stockguidance.json.ContractException;
import stockguidance.json.JsonSupport;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;

class InterchangeCodecTest {
    @Test
    void consumesPythonReferenceFixtureWithoutSortingTickerIds() throws IOException {
        String fixture;
        try (InputStream input = getClass().getResourceAsStream("/interchange-v1.json")) {
            fixture = new String(input.readAllBytes(), StandardCharsets.UTF_8);
        }
        JsonNode root = JsonSupport.parse(fixture);
        InterchangeDocument document = InterchangeCodec.decode(root);

        assertEquals(1, document.schemaVersion());
        assertEquals(List.of(7, 2, 11), document.tickerIds());
        assertEquals("2024-07-03", document.sessions().get(0).exchangeDate().toString());
        assertEquals(-125, document.observations().get(0).value().intValue());
        assertNull(document.observations().get(1).value());
        assertEquals("revision-1", document.observations().get(0).revision());
        assertEquals("late_receipt", document.errors().get(0).code());
        assertEquals(List.of(11), document.findings().get(0).tickerIds());
        assertEquals("5e1fa46e012effbb02606d065b410e61a045f7bfddb3b7489f59205e6854ca05", InterchangeCodec.canonicalHash(root));
    }

    @Test
    void canonicalizesEscapedUnicodeNegativeExponentNullAndSortedKeys() {
        JsonNode root = JsonSupport.parse("{\"z\":null,\"n\":-1.25e2,\"a\":\"caf\\u00e9\"}");
        String canonical = new String(CanonicalJson.bytes(root), StandardCharsets.UTF_8);

        assertEquals("{\"a\":\"caf\\u00e9\",\"n\":-125,\"z\":null}\n", canonical);
    }

    @Test
    void usesTheDocumentedMoneyRoundingRule() {
        assertEquals("1.2346", new Money("1.23456").amount().toPlainString());
        assertEquals("-1.2346", new Money("-1.23455").amount().toPlainString());
    }

    @Test
    void rejectsMissingFieldsUnsupportedVersionsAndNonUtcTimes() {
        String fixture = "{\"schema_version\":1,\"source\":{},\"calendar_id\":\"XNYS\"}";
        assertThrows(ContractException.class, () -> InterchangeCodec.decode(JsonSupport.parse(fixture)));

        String nonUtc = readFixture();
        assertThrows(ContractException.class, () -> InterchangeCodec.decode(JsonSupport.parse(nonUtc.replace("2024-07-03T20:00:00Z", "2024-07-03T20:00:00+00:00"))));
    }

    private String readFixture() {
        try (InputStream input = getClass().getResourceAsStream("/interchange-v1.json")) {
            return new String(input.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException error) {
            throw new IllegalStateException("Fixture could not be read", error);
        }
    }
}
