package stockguidance.migration;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import stockguidance.database.TableNames;
import stockguidance.json.CanonicalJson;
import stockguidance.json.JsonSupport;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class JavaWriterManifest {
    private final Path manifestPath;
    private final JsonNode root;
    private final TableNames tableNames;
    private final String manifestId;
    private final String sourceKey;
    private final String correctedSchemaHash;
    private final Path dataRoot;
    private final Map<LocalDate, FileEntry> files;
    private final Map<LocalDate, TargetEntry> targets;

    private JavaWriterManifest(Path manifestPath, JsonNode root, TableNames tableNames, String manifestId,
                               String sourceKey, String correctedSchemaHash, Path dataRoot,
                               Map<LocalDate, FileEntry> files, Map<LocalDate, TargetEntry> targets) {
        this.manifestPath = manifestPath;
        this.root = root;
        this.tableNames = tableNames;
        this.manifestId = manifestId;
        this.sourceKey = sourceKey;
        this.correctedSchemaHash = correctedSchemaHash;
        this.dataRoot = dataRoot;
        this.files = Map.copyOf(files);
        this.targets = Map.copyOf(targets);
    }

    public static JavaWriterManifest read(Path path) {
        if (path == null) throw new IllegalArgumentException("Manifest path is required");
        JsonNode root = JsonSupport.read(path);
        if (!root.isObject() || root.path("schema_version").asInt(-1) != 1 || root.path("stage").asInt(-1) != 6) {
            throw new IllegalArgumentException("Unsupported Stage 6 manifest");
        }
        String manifestId = requiredText(root, "manifest_id");
        ObjectNode unsigned = (ObjectNode) root.deepCopy();
        unsigned.remove("manifest_id");
        if (!manifestId.equals(CanonicalJson.sha256(unsigned))) {
            throw new IllegalArgumentException("Stage 6 manifest identity changed");
        }
        ObjectNode registry = JsonNodeFactory.instance.objectNode();
        registry.put("schema_version", 1);
        JsonNode sourceRegistry = root.path("authoritative_registry");
        if (!sourceRegistry.isObject() || !sourceRegistry.path("tables").isObject() || !sourceRegistry.path("zscore_templates").isObject()) {
            throw new IllegalArgumentException("Manifest registry is incomplete");
        }
        registry.set("tables", sourceRegistry.path("tables").deepCopy());
        registry.set("zscore_templates", sourceRegistry.path("zscore_templates").deepCopy());
        TableNames names = TableNames.decode(registry);
        String correctedSchemaHash = requiredText(root, "corrected_schema_hash");
        JsonNode inputs = object(root, "inputs");
        Path dataRoot = absolutePath(requiredText(inputs, "data_root"), path);
        Path auditReport = absolutePath(requiredText(inputs, "audit_report"), path);
        JsonNode snapshot = object(root, "snapshot_identity");
        String auditHash = requiredText(snapshot, "audit_sha256");
        if (!auditHash.equals(hash(auditReport))) {
            throw new IllegalArgumentException("Stage 5 audit identity changed");
        }
        String sourceKey = snapshot.path("source_key").isNull() ? null : optionalText(snapshot, "source_key");
        Map<LocalDate, FileEntry> files = readFiles(root.path("files"), path);
        Map<LocalDate, TargetEntry> targets = readTargets(root.path("targets"));
        if (files.isEmpty() || targets.isEmpty()) throw new IllegalArgumentException("Manifest has no candidate sessions");
        return new JavaWriterManifest(path.toAbsolutePath().normalize(), root, names, manifestId, sourceKey,
                correctedSchemaHash, dataRoot, files, targets);
    }

    public Path manifestPath() {
        return manifestPath;
    }

    public JsonNode root() {
        return root.deepCopy();
    }

    public TableNames tableNames() {
        return tableNames;
    }

    public String manifestId() {
        return manifestId;
    }

    public String sourceKey() {
        return sourceKey;
    }

    public String correctedSchemaHash() {
        return correctedSchemaHash;
    }

    public String sourceScope() {
        return CanonicalJson.sha256(root.path("source").path("archive_source"));
    }

    public String calendarId() {
        return "XNYS";
    }

    public Map<Long, String> tickerMappings() {
        JsonNode values = root.path("mappings").path("ticker_mappings");
        if (!values.isArray()) throw new IllegalArgumentException("Manifest ticker mappings are required");
        Map<Long, String> result = new LinkedHashMap<>();
        for (JsonNode value : values) {
            if (!value.path("id").canConvertToLong() || value.path("id").longValue() < 1 || !value.path("label").isTextual()) {
                throw new IllegalArgumentException("Invalid manifest ticker mapping");
            }
            if (result.putIfAbsent(value.path("id").longValue(), value.path("label").textValue()) != null) {
                throw new IllegalArgumentException("Duplicate manifest ticker mapping");
            }
        }
        return Map.copyOf(result);
    }

    public Path dataRoot() {
        return dataRoot;
    }

    public FileEntry file(LocalDate session) {
        FileEntry entry = files.get(session);
        if (entry == null) throw new IllegalArgumentException("Manifest has no session file: " + session);
        return entry;
    }

    public TargetEntry target(LocalDate session) {
        TargetEntry entry = targets.get(session);
        if (entry == null) throw new IllegalArgumentException("Manifest has no target: " + session);
        return entry;
    }

    public ArchiveCandidateReader.SessionArchive archive(LocalDate session) {
        return archive(session, ArchiveCandidateReader.Parser.JACKSON);
    }

    public ArchiveCandidateReader.SessionArchive archive(LocalDate session, ArchiveCandidateReader.Parser parser) {
        FileEntry entry = file(session);
        if (!entry.valid()) throw new InvalidSourceException("Archive session is not adoptable: " + session);
        if (entry.path() == null) throw new InvalidSourceException("Archive path is unavailable: " + session);
        ArchiveCandidateReader.SessionArchive archive = ArchiveCandidateReader.read(entry.path(), session, parser);
        if (entry.fileSha256() == null || !entry.fileSha256().equals(archive.fileSha256())) {
            throw new IllegalArgumentException("Changed Stage 6 input: " + session);
        }
        if (entry.recordsId() == null || !entry.recordsId().equals(archive.recordsId())) {
            throw new IllegalArgumentException("Changed Stage 6 record identity: " + session);
        }
        return archive;
    }

    public void verifyArchiveIdentity(ArchiveCandidateReader.SessionArchive archive) {
        FileEntry entry = file(archive.session());
        if (!entry.valid() || entry.path() == null || entry.fileSha256() == null || entry.recordsId() == null
                || !entry.fileSha256().equals(archive.fileSha256()) || !entry.recordsId().equals(archive.recordsId())
                || !entry.fileSha256().equals(hash(entry.path()))) {
            throw new IllegalArgumentException("Changed Stage 6 input: " + archive.session());
        }
    }

    public boolean isAdoptable(LocalDate session) {
        return file(session).valid();
    }

    private static Map<LocalDate, FileEntry> readFiles(JsonNode value, Path manifestPath) {
        if (!value.isArray()) throw new IllegalArgumentException("Manifest files are required");
        Map<LocalDate, FileEntry> result = new HashMap<>();
        for (JsonNode item : value) {
            LocalDate session = date(item, "session");
            if (result.containsKey(session)) throw new IllegalArgumentException("Duplicate manifest session file");
            String rawPath = item.path("path").isNull() ? null : optionalText(item, "path");
            Path archivePath = rawPath == null ? null : absolutePath(rawPath, manifestPath);
            String hash = optionalText(item, "file_sha256");
            String recordsId = optionalText(item, "records_id");
            if (item.path("valid").asBoolean(false) && (archivePath == null || hash == null || recordsId == null)) {
                throw new IllegalArgumentException("Valid manifest source lacks an identity");
            }
            result.put(session, new FileEntry(session, archivePath, item.path("valid").asBoolean(false), hash, recordsId));
        }
        return result;
    }

    private static Map<LocalDate, TargetEntry> readTargets(JsonNode value) {
        if (!value.isArray()) throw new IllegalArgumentException("Manifest targets are required");
        Map<LocalDate, TargetEntry> result = new HashMap<>();
        for (JsonNode item : value) {
            LocalDate session = date(item, "session");
            if (result.containsKey(session)) throw new IllegalArgumentException("Duplicate manifest target");
            List<LocalDate> dependencies = new ArrayList<>();
            JsonNode values = item.path("derived_dependencies");
            if (!values.isArray()) throw new IllegalArgumentException("Derived dependencies are required");
            for (JsonNode dependency : values) dependencies.add(LocalDate.parse(requiredText(dependency)));
            List<LocalDate> zScoreDependencies = new ArrayList<>();
            JsonNode zScoreValues = item.path("zscore_dependencies");
            if (zScoreValues.isArray()) {
                for (JsonNode dependency : zScoreValues) zScoreDependencies.add(LocalDate.parse(requiredText(dependency)));
            }
            result.put(session, new TargetEntry(session, List.copyOf(dependencies), List.copyOf(zScoreDependencies)));
        }
        return result;
    }

    private static JsonNode object(JsonNode root, String name) {
        JsonNode value = root.get(name);
        if (value == null || !value.isObject()) throw new IllegalArgumentException("Manifest object is missing: " + name);
        return value;
    }

    private static String requiredText(JsonNode root, String name) {
        return requiredText(root.get(name));
    }

    private static String requiredText(JsonNode value) {
        if (value == null || !value.isTextual() || value.textValue().isBlank()) {
            throw new IllegalArgumentException("Manifest text is missing");
        }
        return value.textValue();
    }

    private static String optionalText(JsonNode root, String name) {
        JsonNode value = root.get(name);
        return value == null || value.isNull() ? null : requiredText(value);
    }

    private static LocalDate date(JsonNode root, String name) {
        return LocalDate.parse(requiredText(root, name));
    }

    private static Path absolutePath(String value, Path relativeTo) {
        Path path = Path.of(value);
        if (path.isAbsolute()) return path.normalize();
        Path base = Files.isDirectory(relativeTo) ? relativeTo : relativeTo.toAbsolutePath().getParent();
        return base.resolve(path).normalize();
    }

    private static String hash(Path path) {
        MessageDigest digest;
        try {
            digest = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-256 is unavailable", error);
        }
        try (var input = Files.newInputStream(path)) {
            byte[] buffer = new byte[1024 * 1024];
            int count;
            while ((count = input.read(buffer)) >= 0) {
                if (count > 0) digest.update(buffer, 0, count);
            }
        } catch (IOException error) {
            throw new IllegalArgumentException("Cannot read manifest audit evidence", error);
        }
        return java.util.HexFormat.of().formatHex(digest.digest());
    }

    public record FileEntry(LocalDate session, Path path, boolean valid, String fileSha256, String recordsId) {
    }

    public record TargetEntry(LocalDate session, List<LocalDate> derivedDependencies, List<LocalDate> zscoreDependencies) {
    }

    public static final class InvalidSourceException extends IllegalArgumentException {
        public InvalidSourceException(String message) {
            super(message);
        }
    }
}
