package stockguidance.migration;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
import stockguidance.database.SqlIdentifiers;
import stockguidance.database.TableNames;
import stockguidance.json.CanonicalJson;
import stockguidance.json.JsonSupport;
import stockguidance.validation.ValidationContent;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.math.MathContext;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

final class RowQuality {
    static final int BAD = 1, SINGLE = 2, GAP = 4, FUTURE_LEAKAGE = 8;
    static final String METHOD = "linear-trading-session-v1";
    static final List<String> FIELDS = List.of("open", "high", "low", "close", "volume", "vwap", "transactions");
    private static final MathContext PRECISION = new MathContext(90, RoundingMode.HALF_UP);

    record Key(long dateId, long tickerId) { }
    record Observed(long dateId, long tickerId, String ticker, LocalDate date, int sequence,
                    List<BigDecimal> values, Instant availableAt) { }
    record Repair(Observed observed, int flags, List<String> fields, List<BigDecimal> effective,
                  int gapLength, LocalDate prior, LocalDate following, Instant availableAt, Instant endpointsAvailableAt, List<List<BigDecimal>> endpointValues) {
        JsonObject payload() {
            JsonObject value = JsonSupport.object();
            value.put("ticker", observed.ticker); value.put("date", observed.date.toString());
            value.put("sequence", observed.sequence); value.put("original", strings(observed.values));
            value.put("observed_at", timestamp(observed.availableAt)); value.put("flags", flags);
            JsonArray names = JsonSupport.array(); names.addAll(fields); value.put("fields", names);
            value.put("effective", effective == null ? null : strings(effective)); value.put("gap_length", gapLength);
            value.put("prior", prior == null ? null : prior.toString()); value.put("following", following == null ? null : following.toString());
            value.put("method", METHOD); value.put("available_at", availableAt == null ? null : timestamp(availableAt));
            value.put("endpoints_available_at", endpointsAvailableAt == null ? null : timestamp(endpointsAvailableAt));
            JsonArray endpoints = JsonSupport.array(); for (List<BigDecimal> values : endpointValues) endpoints.add(strings(values));
            value.put("endpoint_values", endpoints);
            return value;
        }
    }

    private RowQuality() { }

    private static String timestamp(Instant value) {
        String base = java.time.format.DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss").withZone(java.time.ZoneOffset.UTC).format(value);
        int micros = value.getNano() / 1000;
        return base + (micros == 0 ? "" : String.format(java.util.Locale.ROOT, ".%06d", micros)) + "+00:00";
    }

    static List<BigDecimal> values(ValidationContent.RawBar bar) {
        return java.util.Arrays.asList(bar.open(), bar.high(), bar.low(), bar.close(), bar.volume(), bar.vwap(),
                bar.transactions() == null ? null : BigDecimal.valueOf(bar.transactions()));
    }

    static List<String> invalid(List<BigDecimal> values) {
        List<String> fields = new ArrayList<>();
        for (int index = 0; index < 7; index++) {
            BigDecimal value = values.get(index);
            if (value == null && index >= 5) continue;
            boolean bad = value == null || (index < 4 || index == 5 ? value.signum() <= 0 || value.compareTo(new BigDecimal("1e60")) >= 0
                    : value.signum() < 0 || value.compareTo(BigDecimal.valueOf(Long.MAX_VALUE)) > 0);
            if (index == 6 && value != null && value.stripTrailingZeros().scale() > 0) bad = true;
            if (bad) fields.add(FIELDS.get(index));
        }
        if (fields.stream().noneMatch(FIELDS.subList(0, 4)::contains)) {
            if (values.get(2).compareTo(values.get(0).min(values.get(3))) > 0
                    || values.get(1).compareTo(values.get(0).max(values.get(3))) < 0 || values.get(2).compareTo(values.get(1)) > 0) {
                fields.addAll(FIELDS.subList(0, 4));
            }
        }
        return List.copyOf(fields);
    }

    static List<Repair> interpolate(List<Observed> input, Instant repairedAt) {
        List<Observed> rows = input.stream().sorted(java.util.Comparator.comparing(Observed::ticker).thenComparingInt(Observed::sequence)).toList();
        List<List<String>> issues = rows.stream().map(row -> invalid(row.values)).toList();
        List<Repair> result = new ArrayList<>();
        for (int index = 0; index < rows.size(); index++) {
            if (issues.get(index).isEmpty()) continue;
            Observed row = rows.get(index);
            int start = index, end = index;
            while (start > 0 && rows.get(start - 1).ticker.equals(row.ticker) && !issues.get(start - 1).isEmpty()
                    && rows.get(start - 1).sequence + 1 == rows.get(start).sequence) start--;
            while (end + 1 < rows.size() && rows.get(end + 1).ticker.equals(row.ticker) && !issues.get(end + 1).isEmpty()
                    && rows.get(end).sequence + 1 == rows.get(end + 1).sequence) end++;
            Observed prior = null, following = null;
            for (int position = start - 1; position >= 0 && rows.get(position).ticker.equals(row.ticker); position--) {
                if (issues.get(position).isEmpty()) { prior = rows.get(position); break; }
            }
            for (int position = end + 1; position < rows.size() && rows.get(position).ticker.equals(row.ticker); position++) {
                if (issues.get(position).isEmpty()) { following = rows.get(position); break; }
            }
            int gap = end - start + 1;
            Repair unresolved = new Repair(row, BAD, issues.get(index), null, gap, null, null, null, null, List.of());
            if (prior == null || following == null) { result.add(unresolved); continue; }
            List<BigDecimal> effective = new ArrayList<>(row.values);
            boolean complete = true;
            BigDecimal weight = BigDecimal.valueOf(row.sequence - prior.sequence).divide(BigDecimal.valueOf(following.sequence - prior.sequence), PRECISION);
            for (String field : issues.get(index)) {
                int position = FIELDS.indexOf(field);
                BigDecimal left = prior.values.get(position), right = following.values.get(position);
                if (left == null || right == null) { complete = false; break; }
                effective.set(position, left.add(weight.multiply(right.subtract(left), PRECISION), PRECISION)
                        .setScale(position == 6 ? 0 : position == 5 ? 22 : 8, RoundingMode.HALF_UP));
            }
            if (!complete || !invalid(effective).isEmpty()) { result.add(unresolved); continue; }
            Instant endpoints = Collections.max(List.of(row.availableAt, prior.availableAt, following.availableAt));
            result.add(new Repair(row, BAD | (gap == 1 ? SINGLE : GAP), issues.get(index), effective, gap,
                    prior.date, following.date, endpoints.isAfter(repairedAt) ? endpoints : repairedAt, endpoints, List.of(prior.values, following.values)));
        }
        return List.copyOf(result);
    }

    static Map<Key, JsonObject> load(Connection connection, List<Long> dateIds) throws SQLException {
        return load(connection, dateIds, false);
    }

    static Map<Key, JsonObject> load(Connection connection, List<Long> dateIds, boolean lock) throws SQLException {
        Map<Key, JsonObject> result = new HashMap<>();
        if (dateIds.isEmpty()) return result;
        String sql = "SELECT date_id,ticker_id,payload FROM stockguidance_row_quality WHERE date_id IN ("
                + String.join(",", Collections.nCopies(dateIds.size(), "?")) + ")" + (lock ? " FOR UPDATE" : "");
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            for (int index = 0; index < dateIds.size(); index++) statement.setLong(index + 1, dateIds.get(index));
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) result.put(new Key(rows.getLong(1), rows.getLong(2)), JsonSupport.parse(rows.getString(3)));
            }
        }
        return result;
    }

    static void reconcile(Connection connection, JavaWriterManifest manifest, List<LocalDate> dates,
                          Map<LocalDate, OperationalDatabase.Receipt> receipts) throws SQLException {
        if (dates.isEmpty()) return;
        TableNames names = manifest.tableNames();
        JsonObject sequences = manifest.root().get("session_sequences") instanceof JsonObject supplied ? supplied : JsonSupport.object();
        Map<Long, LocalDate> dateIds = new HashMap<>();
        Map<Long, String> tickers = new HashMap<>();
        try (PreparedStatement statement = connection.prepareStatement("SELECT id, transaction_date FROM " + SqlIdentifiers.quote(names.sessions(), "sessions")); ResultSet rows = statement.executeQuery()) {
            while (rows.next()) if (dates.contains(rows.getDate(2).toLocalDate())) dateIds.put(rows.getLong(1), rows.getDate(2).toLocalDate());
        }
        if (dateIds.isEmpty()) return;
        try (PreparedStatement statement = connection.prepareStatement("SELECT id,label FROM " + SqlIdentifiers.quote(names.tickers(), "tickers")); ResultSet rows = statement.executeQuery()) {
            while (rows.next()) tickers.put(rows.getLong(1), rows.getString(2));
        }
        Map<Key, JsonObject> known = load(connection, List.copyOf(dateIds.keySet()));
        java.util.Set<Long> affectedTickers = new java.util.HashSet<>();
        for (Key key : known.keySet()) affectedTickers.add(key.tickerId);
        String dateParameters = String.join(",", Collections.nCopies(dateIds.size(), "?"));
        String invalidSql = "SELECT DISTINCT ticker_id FROM " + SqlIdentifiers.quote(names.prices(), "prices")
                + " WHERE date_id IN (" + dateParameters + ") AND (open IS NULL OR open<=0 OR high IS NULL OR high<=0 OR low IS NULL OR low<=0 OR close IS NULL OR close<=0 OR volume IS NULL OR volume<0 OR vwap<=0 OR transactions<0 OR low>open OR low>close OR high<open OR high<close OR low>high)";
        try (PreparedStatement statement = connection.prepareStatement(invalidSql)) {
            int parameter = 1; for (long dateId : dateIds.keySet()) statement.setLong(parameter++, dateId);
            try (ResultSet rows = statement.executeQuery()) { while (rows.next()) affectedTickers.add(rows.getLong(1)); }
        }
        if (affectedTickers.isEmpty()) return;
        List<Observed> observations = new ArrayList<>();
        String sql = "SELECT date_id,ticker_id,open,high,low,close,volume,vwap,transactions FROM " + SqlIdentifiers.quote(names.prices(), "prices")
                + " WHERE date_id IN (" + dateParameters + ") AND ticker_id IN (" + String.join(",", Collections.nCopies(affectedTickers.size(), "?")) + ") ORDER BY ticker_id,date_id FOR UPDATE";
        try (PreparedStatement statement = connection.prepareStatement(sql)) {
            int parameter = 1;
            for (long dateId : dateIds.keySet()) statement.setLong(parameter++, dateId);
            for (long tickerId : affectedTickers) statement.setLong(parameter++, tickerId);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    long dateId = rows.getLong(1), tickerId = rows.getLong(2);
                    LocalDate date = dateIds.get(dateId);
                    List<BigDecimal> values = new ArrayList<>();
                    for (int column = 3; column <= 9; column++) values.add(rows.getBigDecimal(column));
                    Instant available = receipts.containsKey(date) ? Instant.parse(receipts.get(date).availableAt()) : Instant.now();
                    int sequence = sequences.get(date.toString()) instanceof Number number ? number.intValue() : dates.indexOf(date);
                    observations.add(new Observed(dateId, tickerId, tickers.get(tickerId), date, sequence, values, available));
                }
            }
        }
        known = load(connection, List.copyOf(dateIds.keySet()), true);
        for (int index = 0; index < observations.size(); index++) {
            Observed observation = observations.get(index);
            JsonObject old = known.get(new Key(observation.dateId, observation.tickerId));
            if (old != null) observations.set(index, new Observed(observation.dateId, observation.tickerId, observation.ticker,
                    observation.date, observation.sequence, decimals((JsonArray) old.get("original")), Instant.parse((String) old.get("observed_at"))));
        }
        Instant now = Instant.now().truncatedTo(java.time.temporal.ChronoUnit.MICROS);
        for (Repair repair : interpolate(observations, now)) {
            JsonObject payload = repair.payload();
            JsonObject old = known.get(new Key(repair.observed.dateId, repair.observed.tickerId));
            if (old != null && old.get("effective") != null
                    && (!dates.contains(LocalDate.parse(old.get("prior").toString())) || !dates.contains(LocalDate.parse(old.get("following").toString())))) continue;
            if (old != null) {
                JsonObject comparable = (JsonObject) JsonSupport.deepCopy(old);
                comparable.remove("available_at");
                JsonObject candidate = (JsonObject) JsonSupport.deepCopy(payload); candidate.remove("available_at");
                if (CanonicalJson.sha256(comparable).equals(CanonicalJson.sha256(candidate))) continue;
            }
            String revision = CanonicalJson.sha256(payload), text = new String(CanonicalJson.bytes(payload), StandardCharsets.UTF_8);
            try (PreparedStatement statement = connection.prepareStatement("INSERT IGNORE INTO stockguidance_quality_revisions (date_id,ticker_id,revision,recorded_at,payload) VALUES (?,?,?,?,?)")) {
                statement.setLong(1, repair.observed.dateId); statement.setLong(2, repair.observed.tickerId); statement.setString(3, revision);
                statement.setString(4, now.toString()); statement.setString(5, text); statement.executeUpdate();
            }
            try (PreparedStatement statement = connection.prepareStatement("INSERT INTO stockguidance_row_quality (date_id,ticker_id,revision,flags,payload) VALUES (?,?,?,?,?) ON DUPLICATE KEY UPDATE revision=VALUES(revision),flags=VALUES(flags),payload=VALUES(payload)")) {
                statement.setLong(1, repair.observed.dateId); statement.setLong(2, repair.observed.tickerId); statement.setString(3, revision);
                statement.setInt(4, repair.flags); statement.setString(5, text); statement.executeUpdate();
            }
            try (PreparedStatement statement = connection.prepareStatement("UPDATE " + SqlIdentifiers.quote(names.imports(), "imports") + " SET derived_dependency_id=NULL,derived_content_id=NULL WHERE session_date IN (?,?,?)")) {
                int sequence = dates.indexOf(repair.observed.date);
                statement.setObject(1, dates.get(Math.max(0, sequence - 1))); statement.setObject(2, repair.observed.date);
                statement.setObject(3, dates.get(Math.min(dates.size() - 1, sequence + 1))); statement.executeUpdate();
            }
        }
    }

    static List<BigDecimal> decimals(JsonArray values) {
        List<BigDecimal> result = new ArrayList<>();
        for (Object value : values) result.add(value == null ? null : new BigDecimal(value.toString()));
        return result;
    }

    static void saveDerived(Connection connection, TableNames names, LocalDate prior, LocalDate current, LocalDate following,
                            java.util.Set<String> outputTickers) throws SQLException {
        Map<Long, LocalDate> dates = new HashMap<>();
        Map<String, Long> tickers = new HashMap<>();
        try (PreparedStatement statement = connection.prepareStatement("SELECT id,transaction_date FROM " + SqlIdentifiers.quote(names.sessions(), "sessions") + " WHERE transaction_date IN (?,?,?)")) {
            statement.setObject(1, prior); statement.setObject(2, current); statement.setObject(3, following);
            try (ResultSet rows = statement.executeQuery()) { while (rows.next()) dates.put(rows.getLong(1), rows.getDate(2).toLocalDate()); }
        }
        Long currentId = dates.entrySet().stream().filter(entry -> entry.getValue().equals(current)).map(Map.Entry::getKey).findFirst().orElseThrow();
        try (PreparedStatement statement = connection.prepareStatement("DELETE FROM stockguidance_output_quality WHERE series_key='legacy-derived' AND date_id=?")) {
            statement.setLong(1, currentId); statement.executeUpdate();
        }
        var quality = load(connection, List.copyOf(dates.keySet()));
        if (quality.isEmpty()) return;
        try (PreparedStatement statement = connection.prepareStatement("SELECT id,label FROM " + SqlIdentifiers.quote(names.tickers(), "tickers")); ResultSet rows = statement.executeQuery()) {
            while (rows.next()) tickers.put(rows.getString(2), rows.getLong(1));
        }
        for (String ticker : outputTickers) {
            JsonObject lineage = JsonSupport.object();
            int flags = 0, gap = 0;
            for (int column = 0; column < 16; column++) {
                JsonArray inputs = JsonSupport.array();
                for (var entry : quality.entrySet()) {
                    LocalDate date = dates.get(entry.getKey().dateId);
                    boolean usedDate = column < 7 ? date.equals(prior) || date.equals(current) : column == 7 ? date.equals(current) : date.equals(current) || date.equals(following);
                    JsonObject payload = entry.getValue();
                    boolean usedTicker = ticker.equals(payload.get("ticker")) || ((column == 12 || column == 14) && date.equals(following));
                    if (!usedDate || !usedTicker) continue;
                    int recordFlags = ((Number) payload.get("flags")).intValue(), length = ((Number) payload.get("gap_length")).intValue();
                    flags |= recordFlags; gap = Math.max(gap, length);
                    JsonObject input = JsonSupport.addObject(inputs); input.put("revision", CanonicalJson.sha256(payload));
                    input.put("flags", recordFlags); input.put("gap_length", length);
                }
                if (!inputs.isEmpty()) lineage.put(Integer.toString(column), inputs);
            }
            if (lineage.isEmpty()) continue;
            try (PreparedStatement statement = connection.prepareStatement("INSERT INTO stockguidance_output_quality (series_key,date_id,ticker_id,flags,gap_length,provenance) VALUES ('legacy-derived',?,?,?,?,?)")) {
                statement.setLong(1, currentId); statement.setLong(2, tickers.get(ticker)); statement.setInt(3, flags); statement.setInt(4, gap);
                statement.setString(5, new String(CanonicalJson.bytes(lineage), StandardCharsets.UTF_8)); statement.executeUpdate();
            }
        }
    }

    static void savePending(Connection connection, JavaWriterManifest manifest, ArchiveCandidateReader.SessionArchive archive,
                            long dateId, Map<String, Long> tickerIds) throws SQLException {
        if (archive.badRecords().isEmpty()) return;
        JsonObject root = manifest.root();
        int sequence = root.get("session_sequences") instanceof JsonObject sequences && sequences.get(archive.session().toString()) instanceof Number number ? number.intValue() : 0;
        Instant observedAt = Instant.parse(manifest.availableAt(archive.session()));
        for (var entry : archive.badRecords().entrySet()) {
            Observed observation = new Observed(dateId, tickerIds.get(entry.getKey()), entry.getKey(), archive.session(), sequence, entry.getValue(), observedAt);
            JsonObject payload = new Repair(observation, BAD, invalid(entry.getValue()), null, 0, null, null, null, null, List.of()).payload();
            String revision = CanonicalJson.sha256(payload), text = new String(CanonicalJson.bytes(payload), StandardCharsets.UTF_8);
            try (PreparedStatement statement = connection.prepareStatement("INSERT IGNORE INTO stockguidance_quality_revisions (date_id,ticker_id,revision,recorded_at,payload) VALUES (?,?,?,?,?)")) {
                statement.setLong(1, dateId); statement.setLong(2, observation.tickerId); statement.setString(3, revision);
                statement.setString(4, timestamp(Instant.now())); statement.setString(5, text); statement.executeUpdate();
            }
            try (PreparedStatement statement = connection.prepareStatement("INSERT INTO stockguidance_row_quality (date_id,ticker_id,revision,flags,payload) VALUES (?,?,?,?,?)")) {
                statement.setLong(1, dateId); statement.setLong(2, observation.tickerId); statement.setString(3, revision);
                statement.setInt(4, BAD); statement.setString(5, text); statement.executeUpdate();
            }
        }
    }

    static JavaZScoreCandidate.Cell cell(Double original, JsonObject quality, JsonObject policy) {
        if (quality == null) return new JavaZScoreCandidate.Cell(original);
        int flags = ((Number) quality.get("flags")).intValue();
        int gap = ((Number) quality.get("gap_length")).intValue();
        boolean accepted = "interpolated".equals(policy.get("quality_history")) && quality.get("effective") instanceof JsonArray;
        String cutoffText = (String) policy.get("quality_cutoff");
        Instant cutoff = cutoffText == null ? Instant.now() : Instant.parse(cutoffText);
        String observed = (String) quality.get("observed_at"), available = (String) quality.get("available_at");
        if (observed != null && Instant.parse(observed).isAfter(cutoff)) accepted = false;
        if (available != null && Instant.parse(available).isAfter(cutoff)) {
            if (accepted && Boolean.TRUE.equals(policy.get("retrospective_quality"))) flags |= FUTURE_LEAKAGE;
            else accepted = false;
        }
        Double effective = quality.get("effective") instanceof JsonArray values && values.get(3) != null ? Double.valueOf(values.get(3).toString()) : null;
        return new JavaZScoreCandidate.Cell(effective, flags, gap, CanonicalJson.sha256(quality), accepted);
    }

    static ValidationContent.RawBar effective(ValidationContent.RawBar original, JsonObject quality) {
        if (quality == null) return invalid(values(original)).isEmpty() ? original : null;
        if (!(quality.get("effective") instanceof JsonArray values)) return null;
        String available = (String) quality.get("available_at");
        if (available != null && Instant.parse(available).isAfter(Instant.now())) return null;
        List<BigDecimal> fields = decimals(values);
        return new ValidationContent.RawBar(original.ticker(), fields.get(0), fields.get(1), fields.get(2), fields.get(3), fields.get(4), fields.get(5), fields.get(6) == null ? null : fields.get(6).longValueExact());
    }

    private static JsonArray strings(List<BigDecimal> values) {
        JsonArray result = JsonSupport.array();
        for (BigDecimal value : values) result.add(value == null ? null : value.toString());
        return result;
    }
}
