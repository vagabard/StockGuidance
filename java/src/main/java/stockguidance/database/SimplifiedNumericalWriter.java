package stockguidance.database;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Types;
import java.time.Instant;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

import static stockguidance.database.SimplifiedContracts.CalculationStatus;

/** JDBC target-schema writer for persisted numerical work units. */
public final class SimplifiedNumericalWriter {
    private static final int DERIVED_COLUMN_COUNT = 16;
    private static final List<String> DERIVED_COLUMNS = List.of(
            "open_ratio_to_prior", "high_ratio_to_prior", "low_ratio_to_prior", "close_ratio_to_prior",
            "volume_ratio_to_prior", "vwap_ratio_to_prior", "transactions_ratio_to_prior", "change_in_price_over_day",
            "next_day_change_in_price", "zero_centered_next_day_price_change", "next_day_price_change_to_extremes_0_1",
            "next_day_raise1_lower0", "next_day_top100_raise1_lower0", "zero_center_extremes_neg1_pos1",
            "zero_center_top100_1_other_neg1", "zero_center_raise1_lower_neg1");

    public interface Inputs {
        DeltaInput delta(Connection connection, SimplifiedContracts.WorkUnit unit) throws SQLException;

        ZScoreInput zscore(Connection connection, SimplifiedContracts.WorkUnit unit) throws SQLException;
    }

    public record DeltaInput(long dateId, LocalDate sessionDate, List<SimplifiedNumericalKernel.DeltaValue> rows,
                             Instant completedAt) {
        public DeltaInput {
            if (dateId <= 0 || sessionDate == null || rows == null || completedAt == null) {
                throw new IllegalArgumentException("Invalid delta input");
            }
            rows = List.copyOf(rows);
        }
    }

    public record ScoreValue(long tickerId, Float score, int status, int qualityFlags, int gapLength,
                             List<String> provenance) {
        public ScoreValue {
            if (tickerId <= 0 || status < 0 || status > 5 || qualityFlags < 0 || gapLength < 0 || provenance == null) {
                throw new IllegalArgumentException("Invalid z-score value");
            }
            if (status == 0 && (score == null || !Float.isFinite(score))) {
                throw new IllegalArgumentException("Ready z-score values must be finite");
            }
            if (status != 0 && score != null) {
                throw new IllegalArgumentException("Non-ready z-score values must be NULL");
            }
            provenance = List.copyOf(provenance);
        }
    }

    public record ZScoreInput(String seriesKey, long dateId, LocalDate sessionDate, String sourceScope,
                              String calendarId, String family, int window, String calculationVersion,
                              String deltaDefinition, String precision, String missingValuePolicy,
                              String dependencyDigest, String availabilityDigest, boolean actualInputAvailable,
                              Instant completedAt, List<ScoreValue> rows, int rowCount, int readyCount,
                              String statusCounts, String contentDigest) {
        public ZScoreInput {
            if (seriesKey == null || dateId <= 0 || sessionDate == null || sourceScope == null || calendarId == null
                    || family == null || window < 2 || calculationVersion == null || deltaDefinition == null
                    || precision == null || missingValuePolicy == null || dependencyDigest == null
                    || availabilityDigest == null || completedAt == null || rows == null || rowCount < 0
                    || readyCount < 0 || readyCount > rowCount || statusCounts == null || contentDigest == null) {
                throw new IllegalArgumentException("Invalid z-score input");
            }
            if (rows.size() != rowCount || rows.stream().filter(value -> value.status() == 0).count() != readyCount) {
                throw new IllegalArgumentException("Z-score counts do not match rows");
            }
            rows = List.copyOf(rows);
        }
    }

    public record RebuildResult(List<SimplifiedContracts.WorkUnit> committed,
                                List<SimplifiedContracts.WorkUnit> explicitNonReady) {
        public RebuildResult {
            committed = List.copyOf(committed);
            explicitNonReady = List.copyOf(explicitNonReady);
        }
    }

    private final TableNames names;

    public SimplifiedNumericalWriter(TableNames names) {
        this.names = Objects.requireNonNull(names, "table names");
    }

    public RebuildResult rebuild(Connection connection, SimplifiedContracts.PendingUpdate operation, Inputs inputs)
            throws SQLException {
        Objects.requireNonNull(connection, "connection");
        Objects.requireNonNull(operation, "operation");
        Objects.requireNonNull(inputs, "inputs");
        if (!operation.affectedRowsCleared()) {
            throw new IllegalArgumentException("Numerical rebuilding is unreachable before clearing");
        }
        List<SimplifiedContracts.WorkUnit> committed = new ArrayList<>();
        List<SimplifiedContracts.WorkUnit> explicitNonReady = new ArrayList<>();
        for (SimplifiedContracts.WorkUnit unit : operation.scope().rebuild()) {
            if (SimplifiedContracts.UnitKind.parse(unit.unitKind()) == SimplifiedContracts.UnitKind.DELTA_SESSION) {
                DeltaInput input = inputs.delta(connection, unit);
                if (!deltaComplete(connection, unit.dateId())) {
                    writeDelta(connection, input);
                }
                committed.add(unit);
                if (input.rows().stream().anyMatch(value -> value.status() != CalculationStatus.READY)) {
                    explicitNonReady.add(unit);
                }
            }
        }
        for (SimplifiedContracts.WorkUnit unit : operation.scope().rebuild()) {
            if (SimplifiedContracts.UnitKind.parse(unit.unitKind()) != SimplifiedContracts.UnitKind.ZSCORE_SESSION) continue;
            ZScoreInput input = inputs.zscore(connection, unit);
            if (!zScoreComplete(connection, input)) {
                writeZScore(connection, input);
            }
            committed.add(unit);
            if (input.rows().stream().anyMatch(value -> value.status() != 0)) explicitNonReady.add(unit);
        }
        return new RebuildResult(committed, explicitNonReady);
    }

    private boolean deltaComplete(Connection connection, long dateId) throws SQLException {
        String table = SqlIdentifiers.quote(names.sessions(), "sessions");
        try (PreparedStatement statement = connection.prepareStatement("SELECT delta_complete FROM " + table + " WHERE id=?")) {
            statement.setLong(1, dateId);
            try (ResultSet rows = statement.executeQuery()) {
                return rows.next() && rows.getBoolean(1);
            }
        }
    }

    private void writeDelta(Connection connection, DeltaInput input) throws SQLException {
        boolean autoCommit = connection.getAutoCommit();
        try {
            connection.setAutoCommit(false);
            String table = SqlIdentifiers.quote(names.deltas(), "deltas");
            String columns = String.join(",", DERIVED_COLUMNS);
            String placeholders = String.join(",", java.util.Collections.nCopies(DERIVED_COLUMN_COUNT + 7, "?"));
            for (SimplifiedNumericalKernel.DeltaValue row : input.rows()) {
                validateDelta(row);
                try (PreparedStatement delete = connection.prepareStatement("DELETE FROM " + table + " WHERE date_id=? AND ticker_id=?")) {
                    delete.setLong(1, input.dateId());
                    delete.setLong(2, row.tickerId());
                    delete.executeUpdate();
                }
                try (PreparedStatement insert = connection.prepareStatement("INSERT INTO " + table + " (ticker_id,date_id," + columns + ",calculation_status,quality_flags,gap_length,provenance,completed_at) VALUES (" + placeholders + ")")) {
                    int index = 1;
                    insert.setLong(index++, row.tickerId());
                    insert.setLong(index++, input.dateId());
                    for (Double value : row.values()) {
                        if (value == null) insert.setNull(index++, Types.DOUBLE); else insert.setDouble(index++, value);
                    }
                    insert.setString(index++, row.status().value());
                    insert.setInt(index++, row.qualityFlags());
                    insert.setInt(index++, row.gapLength());
                    insert.setString(index++, JsonText.array(row.provenance()));
                    insert.setObject(index, input.completedAt());
                    insert.executeUpdate();
                }
            }
            try (PreparedStatement complete = connection.prepareStatement("UPDATE " + SqlIdentifiers.quote(names.sessions(), "sessions") + " SET delta_complete=1, delta_completed_at=? WHERE id=?")) {
                complete.setObject(1, input.completedAt());
                complete.setLong(2, input.dateId());
                complete.executeUpdate();
            }
            connection.commit();
        } catch (RuntimeException | SQLException error) {
            connection.rollback();
            throw error;
        } finally {
            connection.setAutoCommit(autoCommit);
        }
    }

    private void writeZScore(Connection connection, ZScoreInput input) throws SQLException {
        boolean autoCommit = connection.getAutoCommit();
        try {
            connection.setAutoCommit(false);
            ensureSeries(connection, input);
			TableNames.ResolvedZScoreTables resolved = names.resolveZScoreTables(List.of(input.window())).get(input.window());
			String physical = "price_delta".equals(input.family()) ? resolved.priceDelta() : resolved.price();
			String table = SqlIdentifiers.quote(physical, "z-score table");
            for (ScoreValue row : input.rows()) {
                try (PreparedStatement delete = connection.prepareStatement("DELETE FROM " + table + " WHERE date_id=? AND ticker_id=?")) {
                    delete.setLong(1, input.dateId());
                    delete.setLong(2, row.tickerId());
                    delete.executeUpdate();
                }
                try (PreparedStatement insert = connection.prepareStatement("INSERT INTO " + table + " (date_id,ticker_id,zscore,status,quality_flags,gap_length,provenance) VALUES (?,?,?,?,?,?,?)")) {
                    insert.setLong(1, input.dateId());
                    insert.setLong(2, row.tickerId());
                    if (row.score() == null) insert.setNull(3, Types.FLOAT); else insert.setFloat(3, row.score());
                    insert.setInt(4, row.status());
                    insert.setInt(5, row.qualityFlags());
                    insert.setInt(6, row.gapLength());
                    insert.setString(7, JsonText.array(row.provenance()));
                    insert.executeUpdate();
                }
            }
            String metadata = SqlIdentifiers.quote(names.zscoreSessions(), "z-score sessions");
            try (PreparedStatement delete = connection.prepareStatement("DELETE FROM " + metadata + " WHERE series_key=? AND date_id=?")) {
                delete.setString(1, input.seriesKey()); delete.setLong(2, input.dateId()); delete.executeUpdate();
            }
            try (PreparedStatement insert = connection.prepareStatement("INSERT INTO " + metadata + " (series_key,date_id,dependency_digest,availability_digest,actual_input_available,completed_at,row_count,ready_count,status_counts,content_digest) VALUES (?,?,?,?,?,?,?,?,?,?)")) {
                insert.setString(1, input.seriesKey()); insert.setLong(2, input.dateId()); insert.setString(3, input.dependencyDigest());
                insert.setString(4, input.availabilityDigest()); insert.setBoolean(5, input.actualInputAvailable());
                insert.setObject(6, input.completedAt()); insert.setInt(7, input.rowCount()); insert.setInt(8, input.readyCount());
                insert.setString(9, JsonText.statusCounts(input.rows())); insert.setString(10, input.contentDigest()); insert.executeUpdate();
            }
            connection.commit();
        } catch (RuntimeException | SQLException error) {
            connection.rollback();
            throw error;
        } finally {
            connection.setAutoCommit(autoCommit);
        }
    }

    private boolean zScoreComplete(Connection connection, ZScoreInput input) throws SQLException {
        String metadata = SqlIdentifiers.quote(names.zscoreSessions(), "z-score sessions");
        int expectedRows;
        try (PreparedStatement statement = connection.prepareStatement("SELECT row_count FROM " + metadata + " WHERE series_key=? AND date_id=?")) {
            statement.setString(1, input.seriesKey()); statement.setLong(2, input.dateId());
            try (ResultSet rows = statement.executeQuery()) {
                if (!rows.next() || rows.getInt(1) != input.rowCount()) return false;
                expectedRows = rows.getInt(1);
            }
        }
        TableNames.ResolvedZScoreTables resolved = names.resolveZScoreTables(List.of(input.window())).get(input.window());
        String physical = "price_delta".equals(input.family()) ? resolved.priceDelta() : resolved.price();
        String table = SqlIdentifiers.quote(physical, "z-score table");
        try (PreparedStatement statement = connection.prepareStatement("SELECT COUNT(*) FROM " + table + " WHERE date_id=?")) {
            statement.setLong(1, input.dateId());
            try (ResultSet rows = statement.executeQuery()) {
                return rows.next() && rows.getInt(1) == expectedRows;
            }
        }
    }

    private void ensureSeries(Connection connection, ZScoreInput input) throws SQLException {
        String table = SqlIdentifiers.quote(names.zscoreSeries(), "z-score series");
        try (PreparedStatement statement = connection.prepareStatement("SELECT schema_version,source_scope,calendar_id,family,`window`,calculation_version,delta_definition,`precision`,missing_value_policy FROM " + table + " WHERE series_key=?")) {
            statement.setString(1, input.seriesKey());
            try (ResultSet rows = statement.executeQuery()) {
                if (rows.next()) {
                    if (rows.getInt(1) != 1 || !input.sourceScope().equals(rows.getString(2)) || !input.calendarId().equals(rows.getString(3))
                            || !input.family().equals(rows.getString(4)) || rows.getInt(5) != input.window()
                            || !input.calculationVersion().equals(rows.getString(6)) || !input.deltaDefinition().equals(rows.getString(7))
                            || !input.precision().equals(rows.getString(8)) || !input.missingValuePolicy().equals(rows.getString(9))) {
                        throw new IllegalArgumentException("Conflicting z-score series definition");
                    }
                    if (rows.next()) throw new IllegalArgumentException("Duplicate z-score series definition");
                    return;
                }
            }
        }
        try (PreparedStatement insert = connection.prepareStatement("INSERT INTO " + table + " (series_key,schema_version,source_scope,calendar_id,family,`window`,calculation_version,delta_definition,`precision`,missing_value_policy,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)")) {
            insert.setString(1, input.seriesKey()); insert.setInt(2, 1); insert.setString(3, input.sourceScope()); insert.setString(4, input.calendarId());
            insert.setString(5, input.family()); insert.setInt(6, input.window()); insert.setString(7, input.calculationVersion());
            insert.setString(8, input.deltaDefinition()); insert.setString(9, input.precision()); insert.setString(10, input.missingValuePolicy()); insert.setObject(11, input.completedAt()); insert.executeUpdate();
        }
    }

    private static void validateDelta(SimplifiedNumericalKernel.DeltaValue row) {
        if (row.values().size() != DERIVED_COLUMN_COUNT) throw new IllegalArgumentException("Invalid derived column count");
    }

    private static final class JsonText {
        private static String statusCounts(List<ScoreValue> values) {
            int[] counts = new int[6];
            for (ScoreValue value : values) counts[value.status()]++;
            return "{\"0\":" + counts[0] + ",\"1\":" + counts[1] + ",\"2\":" + counts[2]
                    + ",\"3\":" + counts[3] + ",\"4\":" + counts[4] + ",\"5\":" + counts[5] + "}";
        }

        private static String array(List<String> values) {
            return "[" + values.stream().map(value -> "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\"").collect(java.util.stream.Collectors.joining(",")) + "]";
        }
    }
}
