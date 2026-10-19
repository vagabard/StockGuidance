package stockguidance.database;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.time.LocalDate;

public final class PreparedDateRangeQuery {
    private PreparedDateRangeQuery() {
    }

    public static PreparedStatement prepare(Connection connection, String table, String dateColumn, LocalDate start, LocalDate end) throws SQLException {
        TableNames names = TableNames.loadPackaged();
        String safeTable = SqlIdentifiers.requireAllowed(table, "table", java.util.Set.of(names.sessions()));
        String safeDateColumn = SqlIdentifiers.requireAllowed(dateColumn, "date column", java.util.Set.of("transaction_date"));
        return prepare(connection, names, safeTable, safeDateColumn, start, end);
    }

    public static PreparedStatement prepare(Connection connection, TableNames names, LocalDate start, LocalDate end) throws SQLException {
        if (names == null) {
            throw new IllegalArgumentException("Table names are required");
        }
        return prepare(connection, names, names.sessions(), "transaction_date", start, end);
    }

    private static PreparedStatement prepare(Connection connection, TableNames names, String table, String dateColumn, LocalDate start, LocalDate end) throws SQLException {
        if (connection == null || start == null || end == null || start.isAfter(end)) {
            throw new IllegalArgumentException("Invalid date range query");
        }
        if (names == null) {
            throw new IllegalArgumentException("Table names are required");
        }
        String safeTable = SqlIdentifiers.quote(table, "table");
        String safeDateColumn = SqlIdentifiers.quote(dateColumn, "date column");
        PreparedStatement statement = connection.prepareStatement(
                "SELECT `id`, " + safeDateColumn + " FROM " + safeTable + " " +
                        "WHERE " + safeDateColumn + " >= ? AND " + safeDateColumn + " <= ? " +
                        "ORDER BY " + safeDateColumn);
        statement.setObject(1, start);
        statement.setObject(2, end);
        return statement;
    }
}
