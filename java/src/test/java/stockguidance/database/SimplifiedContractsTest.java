package stockguidance.database;

import org.junit.jupiter.api.Test;
import stockguidance.json.JsonSupport;

import java.nio.file.Path;
import java.time.Instant;
import java.time.LocalDate;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class SimplifiedContractsTest {
    @Test
    void scopeRoundTripRetainsTypedUnitsAndCacheInvalidations() {
        Instant now = Instant.parse("2026-10-26T19:30:00Z");
        var unit = new SimplifiedContracts.WorkUnit("raw_session", 42L, null, LocalDate.of(2024, 7, 1), null, null);
        var scope = new SimplifiedContracts.PendingScope(1, SimplifiedContracts.OperationKind.INITIAL_IMPORT,
                "a".repeat(64), "XNYS", now, now,
                List.of(new SimplifiedContracts.PreparedPayload("payload-1", LocalDate.of(2024, 7, 1), Path.of("temp.ndjson"), "b".repeat(64), "c".repeat(64), now, now)),
                new SimplifiedContracts.ClearScope(List.of(unit), List.of(), List.of(), List.of(), List.of()),
                List.of(unit), List.of(new SimplifiedContracts.CacheInvalidation("dataset", List.of(LocalDate.of(2024, 7, 1)))));

        assertEquals(scope, SimplifiedContracts.decodeScope(SimplifiedContracts.encodeScope(scope)));
    }

    @Test
    void duplicateFieldsAndUnknownVersionsAreRejected() {
        assertThrows(RuntimeException.class, () -> JsonSupport.parse("{\"scope_version\":1,\"scope_version\":1}"));
        assertThrows(RuntimeException.class, () -> SimplifiedContracts.decodeScope(JsonSupport.parse("{\"scope_version\":2}")));
    }
}
