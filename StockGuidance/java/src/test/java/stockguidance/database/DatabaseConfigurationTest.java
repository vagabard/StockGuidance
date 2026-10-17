package stockguidance.database;

import org.junit.jupiter.api.Test;

import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;

class DatabaseConfigurationTest {
    @Test
    void decodesEscapedTextAndSchemaAliasWithoutExposingPassword() throws Exception {
        Path path = Files.createTempFile("stage7-db", ".json");
        Files.writeString(path, "{\"host\":\"db\\\"fixture\",\"schema\":\"fixture_data\",\"user\":\"user\\nname\",\"password\":\"secret-never-log\",\"port\":3307,\"driver\":\"auto\"}");
        DatabaseConfiguration configuration = DatabaseConfiguration.load(path);

        assertEquals("db\"fixture", configuration.host());
        assertEquals("fixture_data", configuration.database());
        assertEquals(configuration.database(), configuration.schema());
        assertEquals(3307, configuration.port());
        assertFalse(configuration.toString().contains("secret-never-log"));
    }

    @Test
    void acceptsEqualDatabaseAndSchemaButRejectsConflictAndUnknownFields() throws Exception {
        Path equal = Files.createTempFile("stage7-db-equal", ".json");
        Files.writeString(equal, "{\"database\":\"fixture_data\",\"schema\":\"fixture_data\",\"user\":\"fixture-user\"}");
        assertEquals("fixture_data", DatabaseConfiguration.load(equal).database());

        Path conflict = Files.createTempFile("stage7-db-conflict", ".json");
        Files.writeString(conflict, "{\"database\":\"one\",\"schema\":\"two\",\"user\":\"fixture-user\"}");
        IllegalArgumentException conflictError = assertThrows(IllegalArgumentException.class, () -> DatabaseConfiguration.load(conflict));
        assertFalse(conflictError.getMessage().contains("two"));

        Path unknown = Files.createTempFile("stage7-db-unknown", ".json");
        Files.writeString(unknown, "{\"database\":\"fixture_data\",\"user\":\"fixture-user\",\"api_key\":\"private-value\"}");
        IllegalArgumentException unknownError = assertThrows(IllegalArgumentException.class, () -> DatabaseConfiguration.load(unknown));
        assertFalse(unknownError.getMessage().contains("private-value"));
    }

    @Test
    void rejectsMissingFieldsWrongTypesAndNonexistentExplicitPath() throws Exception {
        Path missingUser = Files.createTempFile("stage7-db-missing", ".json");
        Files.writeString(missingUser, "{\"database\":\"fixture_data\"}");
        assertThrows(IllegalArgumentException.class, () -> DatabaseConfiguration.load(missingUser));

        Path exponentPort = Files.createTempFile("stage7-db-exponent", ".json");
        Files.writeString(exponentPort, "{\"database\":\"fixture_data\",\"user\":\"fixture-user\",\"port\":1e3}");
        assertThrows(IllegalArgumentException.class, () -> DatabaseConfiguration.load(exponentPort));

        Path nested = Files.createTempDirectory("stage7-db-parent").resolve("missing.json");
        IllegalArgumentException missingError = assertThrows(IllegalArgumentException.class, () -> DatabaseConfiguration.load(nested));
        assertEquals("Cannot read the explicit database JSON configuration", missingError.getMessage());
    }

    @Test
    void connectionFailureDoesNotExposeCredentials() {
        DatabaseConfiguration configuration = new DatabaseConfiguration("bad host", "fixture_data", "fixture-user", "secret-never-log", 3306, DatabaseDriver.AUTO);
        DatabaseAccessException error = assertThrows(DatabaseAccessException.class, () -> new MySqlConnectionFactory().open(configuration));
        assertEquals("Configured MySQL connection failed", error.getMessage());
    }
}
