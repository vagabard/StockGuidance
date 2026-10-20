package stockguidance.migration;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import stockguidance.database.SqlIdentifiers;
import stockguidance.database.TableNames;
import stockguidance.json.CanonicalJson;
import stockguidance.json.JsonSupport;
import stockguidance.validation.LegacyDerivedCalculator;
import stockguidance.validation.ValidationContent;

import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Types;
import java.time.Instant;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

public final class JavaWriterCandidate {
    private static final String TRIAL_RECEIPT = "2026-10-20T00:00:00+00:00";
    private static final String COMPLETION_TABLE = "stage6_completion";
    private static final String METADATA_TABLE = "stage6_disposable_metadata";
    private static final String RAW_SERIES = "raw";
    private static final String DERIVED_SERIES = "legacy-open-to-close-v1";

    private final JavaWriterManifest manifest;
    private final TableNames names;

    public JavaWriterCandidate(JavaWriterManifest manifest) {
        this.manifest = manifest;
        this.names = manifest.tableNames();
    }

    public WriteResult writeRaw(Connection connection, LocalDate session, Path checkpoint,
                                InterruptPoint interruption) throws SQLException {
        ArchiveCandidateReader.SessionArchive archive;
        try {
            archive = manifest.archive(session);
        } catch (JavaWriterManifest.InvalidSourceException error) {
            return new WriteResult("raw", session, "invalid_source", false, 0, null);
        }
        validateCheckpoint(checkpoint, RAW_SERIES, session, archive.recordsId());
        validateTarget(connection);
        boolean committed = false;
        boolean originalAutoCommit = connection.getAutoCommit();
        try {
            connection.setAutoCommit(false);
            writeMetadata(connection);
            long dateId = ensureSession(connection, session);
            Map<String, Long> tickerIds = readTickerIds(connection);
            long nextTickerId = nextTickerId(tickerIds);
            Map<String, StoredBar> existing = readStoredBars(connection, dateId);
            List<NewTicker> newTickers = new ArrayList<>();
            List<RawWrite> inserts = new ArrayList<>();
            List<RawWrite> updates = new ArrayList<>();
            for (ValidationContent.RawBar bar : archive.bars().values()) {
                Long tickerId = tickerIds.get(bar.ticker());
                if (tickerId == null) {
                    tickerId = nextTickerId++;
                    tickerIds.put(bar.ticker(), tickerId);
                    newTickers.add(new NewTicker(tickerId, bar.ticker()));
                }
                RawWrite write = new RawWrite(tickerId, dateId, bar);
                StoredBar old = existing.get(bar.ticker());
                if (old == null) inserts.add(write);
                else if (!old.matches(bar)) updates.add(write);
            }
            batchInsertTickers(connection, newTickers);
            batchInsertBars(connection, inserts);
            batchUpdateBars(connection, updates);
            upsertImport(connection, session, archive.recordsId(), null, null);
            upsertCompletion(connection, RAW_SERIES, session, RAW_SERIES, archive.recordsId());
            manifest.archive(session);
            applyInterruption(interruption, InterruptPoint.BEFORE_COMMIT);
            connection.commit();
            committed = true;
            applyInterruption(interruption, InterruptPoint.AFTER_COMMIT);
            writeCheckpoint(checkpoint, RAW_SERIES, session, archive.recordsId());
            return new WriteResult("raw", session, "complete", true, inserts.size() + updates.size(), archive.recordsId());
        } catch (RuntimeException | SQLException error) {
            if (!committed) rollback(connection);
            throw error;
        } finally {
            restoreAutoCommit(connection, originalAutoCommit);
        }
    }

    public WriteResult writeDerived(Connection connection, LocalDate session, Path checkpoint,
                                    InterruptPoint interruption) throws SQLException {
        ArchiveCandidateReader.SessionArchive current;
        List<LocalDate> dependencies = manifest.target(session).derivedDependencies();
        try {
            current = manifest.archive(session);
        } catch (JavaWriterManifest.InvalidSourceException error) {
            return new WriteResult("derived", session, "missing_dependency", false, 0, null);
        }
        if (dependencies.size() != 2) {
            return new WriteResult("derived", session, "missing_dependency", false, 0, null);
        }
        ArchiveCandidateReader.SessionArchive prior;
        ArchiveCandidateReader.SessionArchive following;
        try {
            prior = manifest.archive(dependencies.get(0));
            following = manifest.archive(dependencies.get(1));
        } catch (JavaWriterManifest.InvalidSourceException error) {
            return new WriteResult("derived", session, "missing_dependency", false, 0, null);
        }
        Map<String, ValidationContent.RawBar> previousRows = new TreeMap<>(prior.bars());
        Map<String, ValidationContent.RawBar> currentRows = new TreeMap<>(current.bars());
        Map<String, ValidationContent.RawBar> followingRows = new TreeMap<>(following.bars());
        Map<String, List<Double>> expected = LegacyDerivedCalculator.expected(previousRows, currentRows, followingRows);
        Map<String, ValidationContent.DerivedRow> rows = new TreeMap<>();
        expected.forEach((ticker, values) -> rows.put(ticker, new ValidationContent.DerivedRow(ticker, values)));
        String contentDigest = derivedContentId(rows);
        validateCheckpoint(checkpoint, DERIVED_SERIES, session, contentDigest);
        validateTarget(connection);
        boolean committed = false;
        boolean originalAutoCommit = connection.getAutoCommit();
        try {
            connection.setAutoCommit(false);
            writeMetadata(connection);
            long dateId = ensureSession(connection, session);
            requireRawImport(connection, current);
            requireRawImport(connection, prior);
            requireRawImport(connection, following);
            String dependencyDigest = dependencyDigest(prior, following);
            deleteDerived(connection, dateId);
            batchInsertDerived(connection, dateId, rows);
            upsertImport(connection, session, current.recordsId(), dependencyDigest, contentDigest);
            upsertCompletion(connection, "derived", session, DERIVED_SERIES, contentDigest);
            manifest.archive(session);
            manifest.archive(dependencies.get(0));
            manifest.archive(dependencies.get(1));
            applyInterruption(interruption, InterruptPoint.BEFORE_COMMIT);
            connection.commit();
            committed = true;
            applyInterruption(interruption, InterruptPoint.AFTER_COMMIT);
            writeCheckpoint(checkpoint, DERIVED_SERIES, session, contentDigest);
            return new WriteResult("derived", session, "complete", true, rows.size(), contentDigest);
        } catch (RuntimeException | SQLException error) {
            if (!committed) rollback(connection);
            throw error;
        } finally {
            restoreAutoCommit(connection, originalAutoCommit);
        }
    }

    public void validateTarget(Connection connection) throws SQLException {
        Map<String, String> metadata = new HashMap<>();
        String sql = "SELECT `key`, `value` FROM " + SqlIdentifiers.quote(METADATA_TABLE, METADATA_TABLE);
        try (PreparedStatement statement = connection.prepareStatement(sql); ResultSet rows = statement.executeQuery()) {
            while (rows.next()) {
                String key = rows.getString(1);
                String value = rows.getString(2);
                if (key == null || value == null || metadata.putIfAbsent(key, value) != null) {
                    throw new IllegalArgumentException("Disposable metadata is not unique");
                }
            }
        }
        if (!"false".equalsIgnoreCase(metadata.get("production_target"))) {
            throw new IllegalArgumentException("Java writer target is not marked disposable");
        }
        String schema = connection.getCatalog();
        if (schema == null || !schema.matches("stockguidance_disposable_[a-z0-9_]{1,39}")
                || !schema.equals(metadata.get("database_schema"))
                || !"mysql".equals(metadata.get("database_engine"))
                || !manifest.correctedSchemaHash().equals(metadata.get("corrected_schema_hash"))) {
            throw new IllegalArgumentException("Disposable MySQL schema identity mismatch");
        }
        List<String> tableNames = new ArrayList<>();
        try (PreparedStatement statement = connection.prepareStatement(
                "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME")) {
            statement.setString(1, schema);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    tableNames.add(rows.getString(1));
                    if (!"InnoDB".equals(rows.getString(2))) {
                        throw new IllegalArgumentException("Disposable tables must be InnoDB");
                    }
                }
            }
        }
        ArrayNode definitions = JsonNodeFactory.instance.arrayNode();
        for (String tableName : tableNames) {
            try (PreparedStatement statement = connection.prepareStatement("SHOW CREATE TABLE " + SqlIdentifiers.quote(tableName, tableName));
                 ResultSet rows = statement.executeQuery()) {
                if (!rows.next()) throw new IllegalArgumentException("Missing disposable DDL");
                ArrayNode definition = definitions.addArray();
                definition.add(tableName);
                definition.add(rows.getString(2).replaceAll(" AUTO_INCREMENT=\\d+", ""));
            }
        }
        if (!CanonicalJson.sha256(definitions).equals(metadata.get("actual_schema_hash"))) {
            throw new IllegalArgumentException("Disposable MySQL DDL changed");
        }
        ArrayNode objects = JsonNodeFactory.instance.arrayNode();
        String[] inventories = {
                "SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = ? ORDER BY TRIGGER_NAME",
                "SELECT ROUTINE_NAME, ROUTINE_DEFINITION FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = ? ORDER BY ROUTINE_NAME"
        };
        for (String inventory : inventories) {
            ArrayNode entries = objects.addArray();
            try (PreparedStatement statement = connection.prepareStatement(inventory)) {
                statement.setString(1, schema);
                try (ResultSet rows = statement.executeQuery()) {
                    while (rows.next()) {
                        ArrayNode entry = entries.addArray();
                        for (int index = 1; index <= rows.getMetaData().getColumnCount(); index++) {
                            String value = rows.getString(index);
                            if (value == null) entry.addNull(); else entry.add(value);
                        }
                    }
                }
            }
        }
        if (!CanonicalJson.sha256(objects).equals(metadata.get("actual_objects_hash"))) {
            throw new IllegalArgumentException("Disposable MySQL routines or triggers changed");
        }
        String registry = metadata.get("registry");
        if (registry == null || !registryMatches(registry)) {
            throw new IllegalArgumentException("Disposable target registry does not match the manifest");
        }
        String existingManifest = metadata.get("candidate_manifest_id");
        if (existingManifest != null && !manifest.manifestId().equals(existingManifest)) {
            throw new IllegalArgumentException("Disposable target belongs to another manifest");
        }
        if (metadata.get("schema_hash") == null || metadata.get("schema_hash").isBlank()) {
            throw new IllegalArgumentException("Disposable target schema identity is missing");
        }
    }

    private boolean registryMatches(String registry) {
        JsonNode value = JsonSupport.parse(registry);
        if (!value.isObject()) return false;
        Map<String, String> expected = names.tables();
        Set<String> fields = new HashSet<>();
        value.fieldNames().forEachRemaining(fields::add);
        if (!fields.equals(expected.keySet())) return false;
        for (Map.Entry<String, String> entry : expected.entrySet()) {
            if (!entry.getValue().equals(value.path(entry.getKey()).asText())) return false;
        }
        return true;
    }

    private void writeMetadata(Connection connection) throws SQLException {
        upsertMetadata(connection, "candidate_manifest_id", manifest.manifestId());
        if (manifest.sourceKey() != null) upsertMetadata(connection, "candidate_source_key", manifest.sourceKey());
        upsertMetadata(connection, "candidate_schema_hash", manifest.correctedSchemaHash());
    }

    private static Map<String, Long> readTickerIds(Connection connection, TableNames names) throws SQLException {
        Map<String, Long> result = new HashMap<>();
        String sql = "SELECT id, label FROM " + SqlIdentifiers.quote(names.tickers(), "tickers") + " ORDER BY id";
        try (PreparedStatement statement = connection.prepareStatement(sql); ResultSet rows = statement.executeQuery()) {
            while (rows.next()) {
                long id = rows.getLong(1);
                String label = rows.getString(2);
                if (rows.wasNull() || label == null || result.putIfAbsent(label, id) != null) {
                    throw new IllegalArgumentException("Ticker mapping is not unique");
                }
            }
        }
        return result;
    }

    private Map<String, Long> readTickerIds(Connection connection) throws SQLException {
        return readTickerIds(connection, names);
    }

    private static long nextTickerId(Map<String, Long> values) {
        long maximum = 0;
        for (long value : values.values()) maximum = Math.max(maximum, value);
        return maximum + 1;
    }

    private Map<String, StoredBar> readStoredBars(Connection connection, long dateId) throws SQLException {
        Map<String, StoredBar> result = new HashMap<>();
        String sql = "SELECT p.ticker_id, t.label, p.open, p.high, p.low, p.close, p.volume, p.vwap, p.transactions FROM "
                + SqlIdentifiers.quote(names.prices(), "prices") + " p JOIN "
                + SqlIdentifiers.quote(names.tickers(), "tickers") + " t ON t.id = p.ticker_id WHERE p.date_id = ?";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setLong(1, dateId);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    long tickerId = rows.getLong(1);
                    String label = rows.getString(2);
                    if (label == null || result.putIfAbsent(label, new StoredBar(tickerId, rows.getString(3), rows.getString(4), rows.getString(5), rows.getString(6), rows.getString(7), rows.getString(8), rows.getString(9))) != null) {
                        throw new IllegalArgumentException("Raw database key is not unique");
                    }
                }
            }
        }
        return result;
    }

    private long ensureSession(Connection connection, LocalDate session) throws SQLException {
        String table = SqlIdentifiers.quote(names.sessions(), "sessions");
        Long id = null;
        try (PreparedStatement statement = connection.prepareStatement("SELECT id FROM " + table + " WHERE transaction_date = ?")) {
            statement.setObject(1, session);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    if (id != null) throw new IllegalArgumentException("Session mapping is not unique");
                    id = rows.getLong(1);
                }
            }
        }
        if (id != null) return id;
        try (PreparedStatement statement = connection.prepareStatement("INSERT INTO " + table + " (transaction_date) VALUES (?)")) {
            statement.setObject(1, session);
            statement.executeUpdate();
        }
        try (PreparedStatement statement = connection.prepareStatement("SELECT id FROM " + table + " WHERE transaction_date = ?")) {
            statement.setObject(1, session);
            try (ResultSet rows = statement.executeQuery()) {
                if (!rows.next()) throw new IllegalArgumentException("Session mapping was not created");
                long result = rows.getLong(1);
                if (rows.next()) throw new IllegalArgumentException("Session mapping is not unique");
                return result;
            }
        }
    }

    private void batchInsertTickers(Connection connection, List<NewTicker> rows) throws SQLException {
        if (rows.isEmpty()) return;
        String sql = "INSERT INTO " + SqlIdentifiers.quote(names.tickers(), "tickers") + " (id, label) VALUES (?, ?)";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            for (NewTicker row : rows) {
                statement.setLong(1, row.id());
                statement.setString(2, row.label());
                statement.addBatch();
            }
            statement.executeBatch();
        }
    }

    private void batchInsertBars(Connection connection, List<RawWrite> rows) throws SQLException {
        if (rows.isEmpty()) return;
        String sql = "INSERT INTO " + SqlIdentifiers.quote(names.prices(), "prices") + " (ticker_id, date_id, open, high, low, close, volume, vwap, transactions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            for (RawWrite row : rows) {
                bindRaw(statement, row);
                statement.addBatch();
            }
            statement.executeBatch();
        }
    }

    private void batchUpdateBars(Connection connection, List<RawWrite> rows) throws SQLException {
        if (rows.isEmpty()) return;
        String sql = "UPDATE " + SqlIdentifiers.quote(names.prices(), "prices") + " SET open = ?, high = ?, low = ?, close = ?, volume = ?, vwap = ?, transactions = ? WHERE ticker_id = ? AND date_id = ?";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            for (RawWrite row : rows) {
                bindDecimal(statement, 1, row.bar().open());
                bindDecimal(statement, 2, row.bar().high());
                bindDecimal(statement, 3, row.bar().low());
                bindDecimal(statement, 4, row.bar().close());
                bindDecimal(statement, 5, row.bar().volume());
                bindDecimal(statement, 6, row.bar().vwap());
                bindLong(statement, 7, row.bar().transactions());
                statement.setLong(8, row.tickerId());
                statement.setLong(9, row.dateId());
                statement.addBatch();
            }
            statement.executeBatch();
        }
    }

    private static void bindRaw(PreparedStatement statement, RawWrite row) throws SQLException {
        statement.setLong(1, row.tickerId());
        statement.setLong(2, row.dateId());
        bindDecimal(statement, 3, row.bar().open());
        bindDecimal(statement, 4, row.bar().high());
        bindDecimal(statement, 5, row.bar().low());
        bindDecimal(statement, 6, row.bar().close());
        bindDecimal(statement, 7, row.bar().volume());
        bindDecimal(statement, 8, row.bar().vwap());
        bindLong(statement, 9, row.bar().transactions());
    }

    private static void bindDecimal(PreparedStatement statement, int index, java.math.BigDecimal value) throws SQLException {
        if (value == null) statement.setNull(index, Types.DECIMAL); else statement.setString(index, ArchiveCandidateReader.canonicalDecimal(value));
    }

    private static void bindLong(PreparedStatement statement, int index, Long value) throws SQLException {
        if (value == null) statement.setNull(index, Types.BIGINT); else statement.setLong(index, value);
    }

    private void deleteDerived(Connection connection, long dateId) throws SQLException {
        String sql = "DELETE FROM " + SqlIdentifiers.quote(names.deltas(), "deltas") + " WHERE date_id = ?";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setLong(1, dateId);
            statement.executeUpdate();
        }
    }

    private void batchInsertDerived(Connection connection, long dateId, Map<String, ValidationContent.DerivedRow> rows) throws SQLException {
        String columns = String.join(", ", LegacyDerivedCalculator.COLUMNS.stream().map(item -> SqlIdentifiers.quote(item, item)).toList());
        String placeholders = "?, ?, " + "?, ".repeat(LegacyDerivedCalculator.COLUMNS.size() - 1) + "?";
        String sql = "INSERT INTO " + SqlIdentifiers.quote(names.deltas(), "deltas") + " (ticker_id, date_id, " + columns + ") VALUES (" + placeholders + ")";
        Map<String, Long> tickerIds;
        try {
            tickerIds = readTickerIds(connection);
        } catch (SQLException error) {
            throw error;
        }
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            for (ValidationContent.DerivedRow row : rows.values()) {
                Long tickerId = tickerIds.get(row.ticker());
                if (tickerId == null) throw new IllegalArgumentException("Derived ticker mapping is missing");
                statement.setLong(1, tickerId);
                statement.setLong(2, dateId);
                for (int index = 0; index < row.values().size(); index++) {
                    Double value = row.values().get(index);
                    if (value == null) statement.setNull(index + 3, Types.DOUBLE); else statement.setDouble(index + 3, value);
                }
                statement.addBatch();
            }
            statement.executeBatch();
        }
    }

    private void requireRawImport(Connection connection, ArchiveCandidateReader.SessionArchive archive) throws SQLException {
        LocalDate session = archive.session();
        String sql = "SELECT raw_id FROM " + SqlIdentifiers.quote(names.imports(), "imports") + " WHERE session_date = ?";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            statement.setObject(1, session);
            try (ResultSet rows = statement.executeQuery()) {
                if (!rows.next() || !archive.recordsId().equals(rows.getString(1)) || rows.next()) {
                    throw new IllegalArgumentException("Raw dependency is not adopted: " + session);
                }
            }
        }
        Map<String, StoredBar> stored = readStoredBars(connection, ensureSession(connection, session));
        if (!stored.keySet().equals(archive.bars().keySet())
                || archive.bars().entrySet().stream().anyMatch(entry -> !stored.get(entry.getKey()).matches(entry.getValue()))) {
            throw new IllegalArgumentException("Stored raw dependency differs from adopted archive: " + session);
        }
    }

    private void upsertImport(Connection connection, LocalDate session, String rawId, String dependencyId, String derivedId) throws SQLException {
        String table = SqlIdentifiers.quote(names.imports(), "imports");
        if (dependencyId == null && derivedId == null) {
            try (PreparedStatement statement = connection.prepareStatement(
                    "SELECT raw_id, derived_dependency_id, derived_content_id FROM " + table + " WHERE session_date = ?")) {
                statement.setObject(1, session);
                try (ResultSet rows = statement.executeQuery()) {
                    if (rows.next() && rawId.equals(rows.getString(1))) {
                        dependencyId = rows.getString(2);
                        derivedId = rows.getString(3);
                    }
                }
            }
        }
        String update = "UPDATE " + table + " SET raw_id = ?, available_at = ?, derived_dependency_id = ?, derived_content_id = ? WHERE session_date = ?";
        try (PreparedStatement statement = connection.prepareStatement(update)) {
            statement.setString(1, rawId);
            statement.setString(2, TRIAL_RECEIPT);
            if (dependencyId == null) statement.setNull(3, Types.VARCHAR); else statement.setString(3, dependencyId);
            if (derivedId == null) statement.setNull(4, Types.VARCHAR); else statement.setString(4, derivedId);
            statement.setObject(5, session);
            if (statement.executeUpdate() != 0) return;
        }
        String insert = "INSERT INTO " + table + " (session_date, raw_id, available_at, derived_dependency_id, derived_content_id) VALUES (?, ?, ?, ?, ?)";
        try (PreparedStatement statement = connection.prepareStatement(insert)) {
            statement.setObject(1, session);
            statement.setString(2, rawId);
            statement.setString(3, TRIAL_RECEIPT);
            if (dependencyId == null) statement.setNull(4, Types.VARCHAR); else statement.setString(4, dependencyId);
            if (derivedId == null) statement.setNull(5, Types.VARCHAR); else statement.setString(5, derivedId);
            statement.executeUpdate();
        }
    }

    private static void upsertCompletion(Connection connection, String workload, LocalDate session, String seriesKey, String digest) throws SQLException {
        String table = SqlIdentifiers.quote(COMPLETION_TABLE, COMPLETION_TABLE);
        String update = "UPDATE " + table + " SET content_digest = ?, committed_at = ? WHERE workload = ? AND session_date = ? AND series_key = ?";
        try (PreparedStatement statement = connection.prepareStatement(update)) {
            statement.setString(1, digest);
            statement.setString(2, TRIAL_RECEIPT);
            statement.setString(3, workload);
            statement.setObject(4, session);
            statement.setString(5, seriesKey);
            if (statement.executeUpdate() != 0) return;
        }
        String insert = "INSERT INTO " + table + " (workload, session_date, series_key, content_digest, committed_at) VALUES (?, ?, ?, ?, ?)";
        try (PreparedStatement statement = connection.prepareStatement(insert)) {
            statement.setString(1, workload);
            statement.setObject(2, session);
            statement.setString(3, seriesKey);
            statement.setString(4, digest);
            statement.setString(5, TRIAL_RECEIPT);
            statement.executeUpdate();
        }
    }

    private static void upsertMetadata(Connection connection, String key, String value) throws SQLException {
        String table = SqlIdentifiers.quote(METADATA_TABLE, METADATA_TABLE);
        try (PreparedStatement statement = connection.prepareStatement("UPDATE " + table + " SET value = ? WHERE `key` = ?")) {
            statement.setString(1, value);
            statement.setString(2, key);
            if (statement.executeUpdate() != 0) return;
        }
        try (PreparedStatement statement = connection.prepareStatement("INSERT INTO " + table + " (`key`, value) VALUES (?, ?)")) {
            statement.setString(1, key);
            statement.setString(2, value);
            statement.executeUpdate();
        }
    }

    private static String dependencyDigest(ArchiveCandidateReader.SessionArchive prior,
                                           ArchiveCandidateReader.SessionArchive following) {
        ObjectNode value = JsonNodeFactory.instance.objectNode();
        value.put("prior", prior.session().toString());
        value.put("following", following.session().toString());
        ArrayNode raw = value.putArray("raw");
        raw.add(prior.recordsId());
        raw.add(following.recordsId());
        return CanonicalJson.sha256(value);
    }

    private static String derivedContentId(Map<String, ValidationContent.DerivedRow> rows) {
        StringBuilder payload = new StringBuilder("[");
        rows.values().stream().sorted(java.util.Comparator.comparing(ValidationContent.DerivedRow::ticker)).forEach(row -> {
            if (payload.length() > 1) payload.append(',');
            String ticker = new String(CanonicalJson.bytes(JsonNodeFactory.instance.textNode(row.ticker())), java.nio.charset.StandardCharsets.UTF_8);
            payload.append('[').append(ticker, 0, ticker.length() - 1).append(",[");
            for (int index = 0; index < row.values().size(); index++) {
                if (index > 0) payload.append(',');
                Double value = row.values().get(index);
                payload.append(value == null ? "null" : pythonFloat(value));
            }
            payload.append("]]");
        });
        payload.append("]\n");
        try {
            return java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256")
                    .digest(payload.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException error) {
            throw new IllegalStateException(error);
        }
    }

    static String pythonFloat(double value) {
        if (!Double.isFinite(value)) throw new IllegalArgumentException("Non-finite derived value");
        if (value == 0) return Double.toString(value);
        java.math.BigDecimal decimal = java.math.BigDecimal.valueOf(value).stripTrailingZeros();
        int exponent = decimal.precision() - decimal.scale() - 1;
        if (exponent < -4 || exponent >= 16) {
            return decimal.movePointLeft(exponent).toPlainString() + "e"
                    + (exponent < 0 ? "-" : "+") + String.format(java.util.Locale.ROOT, "%02d", Math.abs(exponent));
        }
        String text = decimal.toPlainString();
        return text.contains(".") ? text : text + ".0";
    }

    private void validateCheckpoint(Path checkpoint, String workload, LocalDate session, String digest) {
        if (checkpoint == null || !Files.exists(checkpoint)) return;
        JsonNode value = JsonSupport.read(checkpoint);
        if (!value.isObject() || value.path("schema_version").asInt(-1) != 1
                || !workload.equals(value.path("workload").asText())
                || !session.toString().equals(value.path("session").asText())
                || !digest.equals(value.path("content_digest").asText())) {
            throw new IllegalArgumentException("Stale or incompatible Java writer checkpoint");
        }
        if (!manifest.manifestId().equals(value.path("manifest_id").asText())) {
            throw new IllegalArgumentException("Java writer checkpoint manifest is not current");
        }
    }

    private void writeCheckpoint(Path checkpoint, String workload, LocalDate session, String digest) {
        if (checkpoint == null) return;
        ObjectNode value = JsonNodeFactory.instance.objectNode();
        value.put("schema_version", 1);
        value.put("manifest_id", manifest.manifestId());
        value.put("workload", workload);
        value.put("session", session.toString());
        value.put("content_digest", digest);
        value.put("after_commit", true);
        value.put("committed_at", Instant.now().toString());
        try {
            Path parent = checkpoint.toAbsolutePath().normalize().getParent();
            if (parent != null) Files.createDirectories(parent);
            Files.write(checkpoint, CanonicalJson.bytes(value));
        } catch (java.io.IOException error) {
            throw new IllegalArgumentException("Cannot write Java writer checkpoint", error);
        }
    }

    public static void applyInterruption(InterruptPoint requested, InterruptPoint point) {
        if (requested == point) throw new InterruptionException("Injected " + point.name().toLowerCase() + " interruption");
    }

    private static void rollback(Connection connection) {
        try {
            connection.rollback();
        } catch (SQLException ignored) {
        }
    }

    private static void restoreAutoCommit(Connection connection, boolean original) {
        try {
            connection.setAutoCommit(original);
        } catch (SQLException ignored) {
        }
    }

    public enum InterruptPoint {
        NONE,
        BEFORE_COMMIT,
        AFTER_COMMIT
    }

    public record WriteResult(String workload, LocalDate session, String status, boolean committed,
                              int changedRows, String contentDigest) {
    }

    public static final class InterruptionException extends RuntimeException {
        public InterruptionException(String message) {
            super(message);
        }
    }

    private record NewTicker(long id, String label) {
    }

    private record RawWrite(long tickerId, long dateId, ValidationContent.RawBar bar) {
    }

    private record StoredBar(long tickerId, String open, String high, String low, String close,
                             String volume, String vwap, String transactions) {
        private boolean matches(ValidationContent.RawBar bar) {
            return equal(open, bar.open()) && equal(high, bar.high()) && equal(low, bar.low())
                    && equal(close, bar.close()) && equal(volume, bar.volume()) && equal(vwap, bar.vwap())
                    && equal(transactions, bar.transactions());
        }

        private static boolean equal(String stored, java.math.BigDecimal expected) {
            if (expected == null) return stored == null;
            return stored != null && ArchiveCandidateReader.canonicalDecimal(new java.math.BigDecimal(stored)).equals(ArchiveCandidateReader.canonicalDecimal(expected));
        }

        private static boolean equal(String stored, Long expected) {
            return expected == null ? stored == null : stored != null && Long.parseLong(stored) == expected;
        }
    }
}
