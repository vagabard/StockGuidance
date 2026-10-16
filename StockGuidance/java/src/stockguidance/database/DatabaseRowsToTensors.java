package stockguidance.database;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.*;
import java.util.stream.Collectors;

public class DatabaseRowsToTensors {

    public static class GroupedData {
        public long[] dates;
        public double[][] X; // Can be (D, T*F) or (D, T, F) conceptually, but here flattened as per python fetch_grouped_by_date
        public double[][] Y; // (D, T)
        public List<Integer> tickerIds;

        public GroupedData(long[] dates, double[][] X, double[][] Y, List<Integer> tickerIds) {
            this.dates = dates;
            this.X = X;
            this.Y = Y;
            this.tickerIds = tickerIds;
        }
    }

    public static GroupedData fetchGroupedByDate(
            String dbinfoFile,
            Integer limit,
            boolean return2d,
            boolean currentDayChange,
            boolean volumeRatio,
            boolean vwapRatio,
            boolean transactionRatio,
            String dayChangeCentered,
            Integer startDateId,
            Integer endDateId,
            Integer tickerPoolStartDateId,
            Integer tickerPoolEndDateId,
            String primaryFeatureField
    ) throws SQLException, java.io.IOException {
        if (primaryFeatureField == null) primaryFeatureField = "change_in_price_over_day";

        try (Connection conn = DbConnect.getConnection(null, dbinfoFile)) {
            String tickerSelectionSql = createTickerIdSelectionSql(true, tickerPoolStartDateId, tickerPoolEndDateId, true);
            String sql = selectVariableDataSql(dayChangeCentered, currentDayChange, volumeRatio, vwapRatio, transactionRatio, true, startDateId, endDateId, tickerSelectionSql);

            if (limit != null && limit > 0) {
                sql += "\nLIMIT " + limit;
            }

            System.out.println("Executing SQL...");
            Map<Integer, List<RowData>> dataByDate = new TreeMap<>();
            Set<Integer> tickersFound = new TreeSet<>();

            try (PreparedStatement stmt = conn.prepareStatement(sql);
                 ResultSet rs = stmt.executeQuery()) {

                while (rs.next()) {
                    int tickerId = rs.getInt("ticker_id");
                    int dateId = rs.getInt("date_id");
                    double primaryFeature = rs.getDouble(primaryFeatureField);
                    double nextDayChange = rs.getDouble("next_day_change_in_price");

                    tickersFound.add(tickerId);

                    List<Double> features = new ArrayList<>();
                    features.add(primaryFeature);

                    // The order of additional fields must match the SQL build
                    if (currentDayChange) features.add(rs.getDouble("change_in_price_over_day"));
                    if (volumeRatio) features.add(rs.getDouble("volume_normalized"));
                    if (vwapRatio) features.add(rs.getDouble("vwap_ratio_to_prior"));
                    if (transactionRatio) features.add(rs.getDouble("transactions_normalized"));

                    dataByDate.computeIfAbsent(dateId, k -> new ArrayList<>())
                            .add(new RowData(tickerId, features, nextDayChange));
                }
            }

            List<Integer> tickerIdsList = new ArrayList<>(tickersFound);
            Map<Integer, Integer> tickerToIndex = new HashMap<>();
            for (int i = 0; i < tickerIdsList.size(); i++) {
                tickerToIndex.put(tickerIdsList.get(i), i);
            }

            int numDates = dataByDate.size();
            int numTickers = tickerIdsList.size();
            int numFeatures = 1 + (currentDayChange ? 1 : 0) + (volumeRatio ? 1 : 0) + (vwapRatio ? 1 : 0) + (transactionRatio ? 1 : 0);

            long[] dates = new long[numDates];
            double[][] X;
            if (return2d) {
                X = new double[numDates][numTickers * numFeatures]; // conceptually 3D but we can represent it flattened or as needed
                // For simplicity let's stick to what python fetch_grouped_by_date returns:
                // if return_2d is False -> shape (D, T*F)
                // if return_2d is True -> shape (D, T, F)
                // In Java double[][] is an array of arrays.
            } else {
                X = new double[numDates][numTickers * numFeatures];
            }
            double[][] Y = new double[numDates][numTickers];

            int dIdx = 0;
            for (Map.Entry<Integer, List<RowData>> entry : dataByDate.entrySet()) {
                dates[dIdx] = entry.getKey();
                Arrays.fill(Y[dIdx], Double.NaN);
                Arrays.fill(X[dIdx], Double.NaN);

                for (RowData row : entry.getValue()) {
                    Integer tIdx = tickerToIndex.get(row.tickerId);
                    if (tIdx != null) {
                        Y[dIdx][tIdx] = row.label;
                        int base = tIdx * numFeatures;
                        for (int f = 0; f < numFeatures; f++) {
                            X[dIdx][base + f] = row.features.get(f);
                        }
                    }
                }
                dIdx++;
            }

            // Label transformation would go here if needed
            // transformLabels(dayChangeCentered, dataByDate, ...)

            return new GroupedData(dates, X, Y, tickerIdsList);
        }
    }

    private static class RowData {
        int tickerId;
        List<Double> features;
        double label;

        RowData(int tickerId, List<Double> features, double label) {
            this.tickerId = tickerId;
            this.features = features;
            this.label = label;
        }
    }

    private static String selectVariableDataSql(String dayChangeCentered, boolean currentDayChange, boolean volumeRatio, boolean vwapRatio, boolean transactionRatio, boolean onlyUseTickersWithoutMissingDates, Integer startDateId, Integer endDateId, String tickerSelectionSql) {
        StringBuilder additionalFields = new StringBuilder("ddsd.next_day_change_in_price");
        if (currentDayChange) additionalFields.append(", ddsd.change_in_price_over_day");
        if (volumeRatio) additionalFields.append(", ddsd.volume_normalized");
        if (vwapRatio) additionalFields.append(", ddsd.vwap_ratio_to_prior");
        if (transactionRatio) additionalFields.append(", ddsd.transactions_normalized");

        StringBuilder fullQuery = new StringBuilder();
        fullQuery.append("SELECT ddsd.ticker_id, ddsd.date_id, ddsd.change_in_price_over_day, ")
                .append(additionalFields)
                .append(" FROM daily_delta_stock_data ddsd ");

        if (onlyUseTickersWithoutMissingDates) {
            fullQuery.append("INNER JOIN ticker_symbol ts ON ddsd.ticker_id = ts.id AND ts.missing_dates = FALSE ");
        }

        fullQuery.append("INNER JOIN (").append(tickerSelectionSql).append(") AS boundary_tickers ON ddsd.ticker_id = boundary_tickers.ticker_id ");

        boolean hasStart = startDateId != null;
        boolean hasEnd = endDateId != null;
        if (hasStart || hasEnd) {
            fullQuery.append("WHERE ");
            if (hasStart) {
                fullQuery.append("ddsd.date_id > ").append(startDateId).append(" ");
            }
            if (hasEnd) {
                if (hasStart) fullQuery.append("AND ");
                fullQuery.append("ddsd.date_id <= ").append(endDateId).append(" ");
            }
        }
        fullQuery.append("ORDER BY ddsd.date_id ASC, ddsd.ticker_id ASC");
        return fullQuery.toString();
    }

    private static String createTickerIdSelectionSql(boolean onlyUseTickersWithoutMissingDates, Integer startDateId, Integer endDateId, boolean onlyUseTickersWithCommonStockType) {
        StringBuilder sql = new StringBuilder("SELECT Distinct(t1.ticker_id) FROM daily_delta_stock_data t1 JOIN ticker_symbol ts ON t1.ticker_id = ts.id ");

        if (startDateId != null || endDateId != null) {
            sql.append("JOIN daily_delta_stock_data t2 ON t1.ticker_id = t2.ticker_id ");

            String startDateString;
            if (startDateId == null || startDateId < 0) {
                startDateString = "(SELECT MIN(date_id) FROM daily_delta_stock_data)";
            } else {
                startDateString = "(SELECT MAX(date_id) FROM daily_delta_stock_data WHERE date_id <= " + startDateId + ")";
            }

            String endDateString;
            if (endDateId == null || endDateId < 0) {
                endDateString = "(SELECT MAX(date_id) FROM daily_delta_stock_data)";
            } else {
                endDateString = "(SELECT MAX(date_id) FROM daily_delta_stock_data WHERE date_id <= " + endDateId + ")";
            }

            sql.append("WHERE t1.date_id = ").append(startDateString).append(" AND t2.date_id = ").append(endDateString).append(" ");
        } else {
            sql.append("WHERE 1=1 "); // placeholder
        }

        if (onlyUseTickersWithoutMissingDates) {
            sql.append("AND ts.missing_dates = FALSE ");
        }
        if (onlyUseTickersWithCommonStockType) {
            sql.append("AND ts.type = 'CS' ");
        }
        return sql.toString();
    }
}
