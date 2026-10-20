package stockguidance.database;

import com.fasterxml.jackson.databind.JsonNode;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import stockguidance.json.ContractException;
import stockguidance.json.JsonSupport;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class TableNamesTest {
    @Test
    void packagedRegistryLoadsAndResolvesDistinctWindows() {
        TableNames names = TableNames.loadPackaged();

        assertEquals("stockguidance_source", names.source());
        assertEquals("zscores_price_20", names.resolveZScoreTables(List.of(20)).get(20).price());
        assertEquals("zscores_price_delta_60", names.resolveZScoreTables(List.of(60)).get(60).priceDelta());
    }

    @Test
    void customRegistryUsesEveryRoleAndRejectsWindowCollisions() {
        TableNames names = TableNames.decode(JsonSupport.parse(validJson("custom_source", "custom_sessions", "custom_tickers", "custom_prices", "custom_deltas", "custom_imports", "custom_series", "custom_sessions_meta", "custom_price_{window}", "custom_delta_{window}")));

        assertEquals("custom_prices", names.table(TableNames.Role.PRICES));
        assertEquals("custom_delta_12", names.resolveZScoreTables(List.of(12)).get(12).priceDelta());
        assertThrows(IllegalArgumentException.class, () -> names.resolveZScoreTables(List.of(12, 12)));
        assertThrows(IllegalArgumentException.class, () -> names.resolveZScoreTables(List.of(1)));
        assertThrows(IllegalArgumentException.class, () -> names.resolveZScoreTables(List.of(2, true)));
        assertThrows(IllegalArgumentException.class, () -> TableNames.decode(JsonSupport.parse(validJson("custom_source", "custom_sessions", "custom_tickers", "custom_price_20", "custom_deltas", "custom_imports", "custom_series", "custom_sessions_meta", "custom_price_{window}", "custom_delta_{window}"))).resolveZScoreTables(List.of(20)));
    }

    @Test
    void strictStructureRejectsUnknownMissingDuplicateAndMalformedValues() {
        String valid = validJson("custom_source", "custom_sessions", "custom_tickers", "custom_prices", "custom_deltas", "custom_imports", "custom_series", "custom_sessions_meta", "custom_price_{window}", "custom_delta_{window}");
        assertThrows(ContractException.class, () -> TableNames.decode(JsonSupport.parse(valid.replace("\"schema_version\":1,", ""))));
        assertThrows(ContractException.class, () -> TableNames.decode(JsonSupport.parse(valid.replace("\"schema_version\":1", "\"schema_version\":2"))));
        assertThrows(RuntimeException.class, () -> TableNames.decode(JsonSupport.parse(valid.replace("\"custom_source\"", "\"custom-source\""))));
        assertThrows(RuntimeException.class, () -> TableNames.decode(JsonSupport.parse(valid.replace("\"custom_price_{window}\"", "\"custom_price_{other}\""))));
        assertThrows(RuntimeException.class, () -> TableNames.decode(JsonSupport.parse(valid.replace("\"custom_imports\"", "\"custom_sessions\""))));
        assertThrows(ContractException.class, () -> JsonSupport.parse(valid.replace("\"schema_version\":1", "\"schema_version\":1,\"schema_version\":1")));
    }

    @Test
    void missingExplicitFileDoesNotFallBackToPackagedResource(@TempDir Path directory) {
        Path missing = directory.resolve("missing-table-names.json");

        assertThrows(ContractException.class, () -> TableNames.load(missing));
    }

    @Test
    void externalRegistryCanBeReadAsUtf8(@TempDir Path directory) throws Exception {
        Path path = directory.resolve("table_names.json");
        Files.writeString(path, validJson("external_source", "custom_sessions", "custom_tickers", "custom_prices", "custom_deltas", "custom_imports", "custom_series", "custom_sessions_meta", "custom_price_{window}", "custom_delta_{window}"));

        assertEquals("external_source", TableNames.load(path).source());
    }

    private static String validJson(String source, String sessions, String tickers, String prices, String deltas, String imports,
                                    String series, String sessionsMeta, String priceTemplate, String deltaTemplate) {
        return "{\"schema_version\":1,\"tables\":{" +
                "\"source\":\"" + source + "\",\"sessions\":\"" + sessions + "\",\"tickers\":\"" + tickers + "\"," +
                "\"prices\":\"" + prices + "\",\"deltas\":\"" + deltas + "\",\"imports\":\"" + imports + "\"," +
                "\"zscore_series\":\"" + series + "\",\"zscore_sessions\":\"" + sessionsMeta + "\"}," +
                "\"zscore_templates\":{\"price\":\"" + priceTemplate + "\",\"price_delta\":\"" + deltaTemplate + "\"}}";
    }
}
