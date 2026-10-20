package stockguidance.interchange;

import com.fasterxml.jackson.databind.JsonNode;
import stockguidance.json.CanonicalJson;
import stockguidance.json.ContractException;
import stockguidance.json.JsonSupport;

import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Set;

public final class InterchangeCodec {
    private static final Set<String> DOCUMENT_FIELDS = Set.of("schema_version", "source", "calendar_id", "calendar_version", "ticker_ids", "sessions", "coverage", "observations", "errors", "findings");
    private static final Set<String> SOURCE_FIELDS = Set.of("provider", "endpoint", "market", "universe", "adjustment", "interval");
    private static final Set<String> SESSION_FIELDS = Set.of("exchange_date", "close_at");
    private static final Set<String> COVERAGE_FIELDS = Set.of("observed_dates", "verified_dates", "holes", "provenance", "provider_state");
    private static final Set<String> OBSERVATION_FIELDS = Set.of("exchange_date", "ticker_id", "value", "receipt", "availability_policy", "revision");
    private static final Set<String> RECEIPT_FIELDS = Set.of("observed_at", "available_at", "content_id");
    private static final Set<String> ERROR_FIELDS = Set.of("code", "message", "exchange_date");
    private static final Set<String> FINDING_FIELDS = Set.of("category", "check", "detail", "session_date", "ticker_ids");

    private InterchangeCodec() {
    }

    public static InterchangeDocument read(java.nio.file.Path path) {
        return decode(JsonSupport.read(path));
    }

    public static InterchangeDocument decode(JsonNode root) {
        fields(root, DOCUMENT_FIELDS);
        int version = integer(root, "schema_version");
        SourceIdentity source = source(object(root, "source"));
        List<Integer> tickerIds = integers(array(root, "ticker_ids"));
        List<ExchangeSession> sessions = sessions(array(root, "sessions"));
        Coverage coverage = coverage(object(root, "coverage"));
        List<Observation> observations = observations(array(root, "observations"));
        List<ErrorRecord> errors = errors(array(root, "errors"));
        List<FindingRecord> findings = findings(array(root, "findings"));
        return new InterchangeDocument(version, source, requiredText(root, "calendar_id"), requiredText(root, "calendar_version"), tickerIds, sessions, coverage, observations, errors, findings);
    }

    public static String canonicalHash(JsonNode root) {
        return CanonicalJson.sha256(root);
    }

    private static SourceIdentity source(JsonNode value) {
        fields(value, SOURCE_FIELDS);
        return new SourceIdentity(requiredText(value, "provider"), requiredText(value, "endpoint"), requiredText(value, "market"), requiredText(value, "universe"), requiredText(value, "adjustment"), requiredText(value, "interval"));
    }

    private static List<ExchangeSession> sessions(JsonNode value) {
        List<ExchangeSession> result = new ArrayList<>();
        for (JsonNode item : value) {
            fields(item, SESSION_FIELDS);
            result.add(new ExchangeSession(date(item, "exchange_date"), instant(item, "close_at")));
        }
        return result;
    }

    private static Coverage coverage(JsonNode value) {
        fields(value, COVERAGE_FIELDS);
        return new Coverage(dates(array(value, "observed_dates")), dates(array(value, "verified_dates")), dates(array(value, "holes")), requiredText(value, "provenance"), requiredText(value, "provider_state"));
    }

    private static List<Observation> observations(JsonNode value) {
        List<Observation> result = new ArrayList<>();
        for (JsonNode item : value) {
            fields(item, OBSERVATION_FIELDS);
            JsonNode valueNode = item.get("value");
            BigDecimal number = valueNode.isNull() ? null : decimal(valueNode, "value");
            String policy = requiredText(item, "availability_policy");
            String revision = optionalText(item, "revision");
            result.add(new Observation(date(item, "exchange_date"), integer(item, "ticker_id"), number, receipt(object(item, "receipt")), policy, revision));
        }
        return result;
    }

    private static Receipt receipt(JsonNode value) {
        fields(value, RECEIPT_FIELDS);
        return new Receipt(instant(value, "observed_at"), instant(value, "available_at"), requiredText(value, "content_id"));
    }

    private static List<ErrorRecord> errors(JsonNode value) {
        List<ErrorRecord> result = new ArrayList<>();
        for (JsonNode item : value) {
            fields(item, ERROR_FIELDS);
            result.add(new ErrorRecord(requiredText(item, "code"), requiredText(item, "message"), optionalDate(item, "exchange_date")));
        }
        return result;
    }

    private static List<FindingRecord> findings(JsonNode value) {
        List<FindingRecord> result = new ArrayList<>();
        for (JsonNode item : value) {
            fields(item, FINDING_FIELDS);
            result.add(new FindingRecord(requiredText(item, "category"), requiredText(item, "check"), requiredText(item, "detail"), optionalDate(item, "session_date"), integers(array(item, "ticker_ids"))));
        }
        return result;
    }

    private static void fields(JsonNode value, Set<String> required) {
        if (value == null || !value.isObject()) {
            throw new ContractException("Interchange object is required");
        }
        Set<String> actual = new HashSet<>();
        Iterator<String> names = value.fieldNames();
        while (names.hasNext()) {
            actual.add(names.next());
        }
        if (!actual.equals(required)) {
            throw new ContractException("Incomplete or unsupported interchange fields");
        }
    }

    private static JsonNode object(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || !value.isObject()) {
            throw new ContractException("Expected an interchange object");
        }
        return value;
    }

    private static JsonNode array(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || !value.isArray()) {
            throw new ContractException("Expected an interchange array");
        }
        return value;
    }

    private static List<Integer> integers(JsonNode value) {
        List<Integer> result = new ArrayList<>();
        for (JsonNode item : value) {
            if (!item.isIntegralNumber() || !item.canConvertToInt()) {
                throw new ContractException("Expected an integer array");
            }
            result.add(item.intValue());
        }
        return result;
    }

    private static List<LocalDate> dates(JsonNode value) {
        List<LocalDate> result = new ArrayList<>();
        for (JsonNode item : value) {
            if (!item.isTextual()) {
                throw new ContractException("Expected exchange dates");
            }
            result.add(parseDate(item.textValue()));
        }
        return result;
    }

    private static LocalDate date(JsonNode root, String name) {
        return parseDate(requiredText(root, name));
    }

    private static LocalDate optionalDate(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || value.isNull()) {
            return null;
        }
        if (!value.isTextual()) {
            throw new ContractException("Expected optional exchange date");
        }
        return parseDate(value.textValue());
    }

    private static LocalDate parseDate(String value) {
        try {
            return LocalDate.parse(value);
        } catch (DateTimeParseException error) {
            throw new ContractException("Invalid exchange date");
        }
    }

    private static Instant instant(JsonNode root, String name) {
        String value = requiredText(root, name);
        if (!value.endsWith("Z")) {
            throw new ContractException("Interchange timestamps must use UTC Z serialization");
        }
        try {
            return Instant.parse(value);
        } catch (DateTimeParseException error) {
            throw new ContractException("Invalid interchange timestamp");
        }
    }

    private static BigDecimal decimal(JsonNode root, String name) {
        if (!root.isNumber()) {
            throw new ContractException("Expected a finite interchange number");
        }
        return root.decimalValue();
    }

    private static int integer(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || !value.isIntegralNumber() || !value.canConvertToInt()) {
            throw new ContractException("Expected an interchange integer");
        }
        return value.intValue();
    }

    private static String requiredText(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || !value.isTextual() || value.textValue().isBlank()) {
            throw new ContractException("Expected an interchange text field");
        }
        return value.textValue();
    }

    private static String optionalText(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || value.isNull()) {
            return null;
        }
        if (!value.isTextual()) {
            throw new ContractException("Expected an optional interchange text field");
        }
        return value.textValue();
    }
}
