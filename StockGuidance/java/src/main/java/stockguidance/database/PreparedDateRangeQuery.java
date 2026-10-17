package stockguidance.database;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.time.LocalDate;
import java.util.Set;

public final class PreparedDateRangeQuery {
    private static final Set<String> TABLES = Set.of("transaction_dates");
    private static final Set<String> DATE_COLUMNS = Set.of("transaction_date");

    private PreparedDateRangeQuery() {
    }

    public static PreparedStatement prepare(Connection connection, String table, String dateColumn, LocalDate start, LocalDate end) throws SQLException {
        if (connection == null || start == null || end == null || start.isAfter(end)) {
            throw new IllegalArgumentException("Invalid date range query");
        }
        String safeTable = SqlIdentifiers.requireAllowed(table, "table", TABLES);
        String safeDateColumn = SqlIdentifiers.requireAllowed(dateColumn, "date column", DATE_COLUMNS);
        PreparedStatement statement = connection.prepareStatement(
                "SELECT `id`, `" + safeDateColumn + "` FROM `" + safeTable + "` " +
                        "WHERE `" + safeDateColumn + "` >= ? AND `" + safeDateColumn + "` <= ? " +
                        "ORDER BY `" + safeDateColumn + "`");
        statement.setObject(1, start);
        statement.setObject(2, end);
        return statement;
    }
}
