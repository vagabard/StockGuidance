package stockguidance.migration;

import org.apache.fory.json.JsonArray;
import org.apache.fory.json.JsonObject;
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
    private final JsonObject root;
    private final TableNames tableNames;
    private final String manifestId;
    private final String sourceKey;
    private final String correctedSchemaHash;
    private final Path dataRoot;
    private final Map<LocalDate, FileEntry> files;
    private final Map<LocalDate, TargetEntry> targets;

    private JavaWriterManifest(Path manifestPath, JsonObject root, TableNames tableNames, String manifestId,
                               String sourceKey, String correctedSchemaHash, Path dataRoot,
                               Map<LocalDate, FileEntry> files, Map<LocalDate, TargetEntry> targets) {
        this.manifestPath = manifestPath;
        this.root = root;
        this.tableNames = tableNames;
        this.manifestId = manifestId;
        this.sourceKey = sourceKey;
        this.correctedSchemaHash = correctedSchemaHash;
        this.dataRoot = dataRoot;
        this.files = new HashMap<>(files);
        this.targets = Map.copyOf(targets);
    }

    public static JavaWriterManifest read(Path path) {
        if (path == null) throw new IllegalArgumentException("Manifest path is required");
        JsonObject root = JsonSupport.read(path);
        if ("daily-maintenance".equals(root.get("kind"))) return readOperational(path, root);
        if (JsonSupport.integerOrDefault(root.get("schema_version"), -1) != 1 || JsonSupport.integerOrDefault(root.get("stage"), -1) != 6) {
            throw new IllegalArgumentException("Unsupported Stage 6 manifest");
        }
        String manifestId = requiredText(root, "manifest_id");
        JsonObject unsigned = (JsonObject) JsonSupport.deepCopy(root);
        unsigned.remove("manifest_id");
        if (!manifestId.equals(CanonicalJson.sha256(unsigned))) {
            throw new IllegalArgumentException("Stage 6 manifest identity changed");
        }
        JsonObject registry = JsonSupport.object();
        registry.put("schema_version", 1);
        Object sourceRegistryValue = root.get("authoritative_registry");
        if (!(sourceRegistryValue instanceof JsonObject sourceRegistry)
                || !(sourceRegistry.get("tables") instanceof JsonObject tables)
                || !(sourceRegistry.get("zscore_templates") instanceof JsonObject zscoreTemplates)) {
            throw new IllegalArgumentException("Manifest registry is incomplete");
        }
        registry.put("tables", JsonSupport.deepCopy(tables));
        registry.put("zscore_templates", JsonSupport.deepCopy(zscoreTemplates));
        TableNames names = TableNames.decode(registry);
        String correctedSchemaHash = requiredText(root, "corrected_schema_hash");
        JsonObject inputs = object(root, "inputs");
        Path dataRoot = absolutePath(requiredText(inputs, "data_root"), path);
        Path auditReport = absolutePath(requiredText(inputs, "audit_report"), path);
        JsonObject snapshot = object(root, "snapshot_identity");
        String auditHash = requiredText(snapshot, "audit_sha256");
        if (!auditHash.equals(hash(auditReport))) {
            throw new IllegalArgumentException("Stage 5 audit identity changed");
        }
        String sourceKey = snapshot.get("source_key") == null ? null : optionalText(snapshot, "source_key");
        Map<LocalDate, FileEntry> files = readFiles(array(root, "files"), path, false);
        Map<LocalDate, TargetEntry> targets = readTargets(array(root, "targets"));
        if (files.isEmpty() || targets.isEmpty()) throw new IllegalArgumentException("Manifest has no candidate sessions");
        return new JavaWriterManifest(path.toAbsolutePath().normalize(), root, names, manifestId, sourceKey,
                correctedSchemaHash, dataRoot, files, targets);
    }

    public Path manifestPath() {
        return manifestPath;
    }

    private static JavaWriterManifest readOperational(Path path, JsonObject root) {
        int version = JsonSupport.integerOrDefault(root.get("schema_version"), -1);
        if (version != 1 && version != 2) {
            throw new IllegalArgumentException("Unsupported daily workload version");
        }
        JsonObject registry = object(root, "authoritative_registry");
        TableNames names = TableNames.decode(registry);
        Map<LocalDate, TargetEntry> targets = readTargets(array(root, "targets"));
        Map<LocalDate, FileEntry> files;
        if (version == 2) {
            if (!"database".equals(root.get("input_mode"))) throw new IllegalArgumentException("Database input mode is required");
            files = new HashMap<>();
            Path directory = absolutePath(requiredText(root, "archive_directory"), path);
            for (Object value : array(root, "input_sessions")) {
                LocalDate session = LocalDate.parse(requiredText(value));
                if (files.putIfAbsent(session, new FileEntry(session, directory.resolve(session + ".ndjson"), false, null, null)) != null) {
                    throw new IllegalArgumentException("Duplicate database input session");
                }
            }
            for (TargetEntry target : targets.values()) {
                for (LocalDate session : target.derivedDependencies()) {
                    files.putIfAbsent(session, new FileEntry(session, null, false, null, null));
                }
            }
            if (root.get("database_sources") instanceof JsonArray sources) {
                for (Object value : sources) {
                    JsonObject item = (JsonObject) value;
                    LocalDate session = date(item, "session");
                    FileEntry previous = files.get(session);
                    if (previous == null) throw new IllegalArgumentException("Out-of-scope database receipt");
                    files.put(session, new FileEntry(session, previous.path(), true, null, requiredText(item, "raw_id")));
                }
            }
        } else {
            files = readFiles(array(root, "files"), path, true);
        }
        JsonObject unsigned = (JsonObject) JsonSupport.deepCopy(root);
        unsigned.remove("manifest_id");
        String identity = CanonicalJson.sha256(unsigned);
        if (!identity.equals(requiredText(root, "manifest_id"))) {
            throw new IllegalArgumentException("Daily workload identity changed");
        }
        return new JavaWriterManifest(path.toAbsolutePath(), root, names, identity,
                requiredText(root, "source_key"), "operational", Path.of(requiredText(root, "data_root")), files, targets);
    }

    public boolean operational() {
        return "daily-maintenance".equals(root.get("kind"));
    }

    public boolean databaseBacked() {
        return operational() && "database".equals(root.get("input_mode"));
    }

    public void databaseSources(Map<LocalDate, OperationalDatabase.Receipt> receipts) {
        JsonArray sources = JsonSupport.array();
        JsonObject availability = (JsonObject) root.get("availability");
        for (LocalDate session : files.keySet().stream().sorted().toList()) {
            FileEntry previous = files.get(session);
            OperationalDatabase.Receipt receipt = receipts.get(session);
            files.put(session, new FileEntry(session, previous.path(), receipt != null, null, receipt == null ? null : receipt.rawId()));
            if (receipt != null) {
                JsonObject item = JsonSupport.addObject(sources);
                item.put("session", session.toString());
                item.put("raw_id", receipt.rawId());
                availability.put(session.toString(), receipt.availableAt());
            }
        }
        root.put("database_sources", sources);
    }

    public String availableAt(LocalDate session) {
        return requiredText(object(root, "availability"), session.toString());
    }

    void availableAt(LocalDate session, String value) {
        object(root, "availability").put(session.toString(), value);
    }

    public JsonObject root() {
        return (JsonObject) JsonSupport.deepCopy(root);
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
        JsonObject source = object(root, "source");
        return CanonicalJson.sha256(object(source, "archive_source"));
    }

    public String calendarId() {
        return requiredText(root, "calendar_id");
    }

    public Map<Long, String> tickerMappings() {
        JsonObject mappings = object(root, "mappings");
        Object valuesValue = mappings.get("ticker_mappings");
        if (!(valuesValue instanceof JsonArray values)) throw new IllegalArgumentException("Manifest ticker mappings are required");
        Map<Long, String> result = new LinkedHashMap<>();
        for (Object rawValue : values) {
            if (!(rawValue instanceof JsonObject value)) throw new IllegalArgumentException("Manifest ticker mapping must be an object");
            Object id = value.get("id");
            Object label = value.get("label");
            if (!JsonSupport.fitsLong(id) || JsonSupport.longIntegerValue(id) < 1 || !JsonSupport.isString(label)) {
                throw new IllegalArgumentException("Invalid manifest ticker mapping");
            }
            if (result.putIfAbsent(JsonSupport.longIntegerValue(id), JsonSupport.stringValue(label)) != null) {
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
        FileEntry entry = file(session);
        if (!entry.valid()) throw new InvalidSourceException("Archive session is not adoptable: " + session);
        if (entry.path() == null) throw new InvalidSourceException("Archive path is unavailable: " + session);
        ArchiveCandidateReader.SessionArchive archive = ArchiveCandidateReader.read(entry.path(), session);
        if (entry.fileSha256() != null && !entry.fileSha256().equals(archive.fileSha256())) {
            throw new IllegalArgumentException("Changed Stage 6 input: " + session);
        }
        if (!trustsExistingFile(session) && entry.recordsId() != null && !entry.recordsId().equals(archive.recordsId())) {
            throw new IllegalArgumentException("Changed Stage 6 record identity: " + session);
        }
        return archive;
    }

    public boolean trustsExistingFile(LocalDate session) {
        FileEntry entry = file(session);
        return operational() && entry.fileSha256() == null;
    }

    public void verifyArchiveIdentity(ArchiveCandidateReader.SessionArchive archive) {
        FileEntry entry = file(archive.session());
        if (databaseBacked()) {
            if (!java.util.Objects.equals(entry.path(), archive.path())) throw new IllegalArgumentException("Unexpected source path");
            return;
        }
        if (!entry.valid() || entry.path() == null
                || (entry.fileSha256() != null && !entry.fileSha256().equals(archive.fileSha256()))
                || (!trustsExistingFile(archive.session()) && entry.recordsId() != null && !entry.recordsId().equals(archive.recordsId()))
                || (entry.fileSha256() != null && !entry.fileSha256().equals(hash(entry.path())))) {
            throw new IllegalArgumentException("Changed Stage 6 input: " + archive.session());
        }
    }

    public boolean isAdoptable(LocalDate session) {
        return file(session).valid();
    }

    private static Map<LocalDate, FileEntry> readFiles(JsonArray value, Path manifestPath, boolean operational) {
        Map<LocalDate, FileEntry> result = new HashMap<>();
        for (Object rawItem : value) {
            if (!(rawItem instanceof JsonObject item)) throw new IllegalArgumentException("Manifest file must be an object");
            LocalDate session = date(item, "session");
            if (result.containsKey(session)) throw new IllegalArgumentException("Duplicate manifest session file");
            String rawPath = item.get("path") == null ? null : optionalText(item, "path");
            Path archivePath = rawPath == null ? null : absolutePath(rawPath, manifestPath);
            String hash = optionalText(item, "file_sha256");
            String recordsId = optionalText(item, "records_id");
            boolean valid = JsonSupport.booleanOrDefault(item.get("valid"), false);
            if (valid && (archivePath == null || (!operational && (hash == null || recordsId == null)))) {
                throw new IllegalArgumentException("Valid manifest source lacks an identity");
            }
            result.put(session, new FileEntry(session, archivePath, valid, hash, recordsId));
        }
        return result;
    }

    private static Map<LocalDate, TargetEntry> readTargets(JsonArray value) {
        Map<LocalDate, TargetEntry> result = new HashMap<>();
        for (Object rawItem : value) {
            if (!(rawItem instanceof JsonObject item)) throw new IllegalArgumentException("Manifest target must be an object");
            LocalDate session = date(item, "session");
            if (result.containsKey(session)) throw new IllegalArgumentException("Duplicate manifest target");
            List<LocalDate> dependencies = new ArrayList<>();
            Object valuesValue = item.get("derived_dependencies");
            if (!(valuesValue instanceof JsonArray values)) throw new IllegalArgumentException("Derived dependencies are required");
            for (Object dependency : values) dependencies.add(LocalDate.parse(requiredText(dependency)));
            List<LocalDate> zScoreDependencies = new ArrayList<>();
            Object zScoreValuesValue = item.get("zscore_dependencies");
            if (zScoreValuesValue instanceof JsonArray zScoreValues) {
                for (Object dependency : zScoreValues) zScoreDependencies.add(LocalDate.parse(requiredText(dependency)));
            }
            result.put(session, new TargetEntry(session, List.copyOf(dependencies), List.copyOf(zScoreDependencies)));
        }
        return result;
    }

    private static JsonObject object(JsonObject root, String name) {
        Object value = root.get(name);
        if (!(value instanceof JsonObject object)) throw new IllegalArgumentException("Manifest object is missing: " + name);
        return object;
    }

    private static String requiredText(JsonObject root, String name) {
        return requiredText(root.get(name));
    }

    private static String requiredText(Object value) {
        if (!JsonSupport.isString(value) || JsonSupport.stringValue(value).isBlank()) {
            throw new IllegalArgumentException("Manifest text is missing");
        }
        return JsonSupport.stringValue(value);
    }

    private static String optionalText(JsonObject root, String name) {
        Object value = root.get(name);
        return value == null ? null : requiredText(value);
    }

    private static LocalDate date(JsonObject root, String name) {
        return LocalDate.parse(requiredText(root, name));
    }

    private static JsonArray array(JsonObject root, String name) {
        Object value = root.get(name);
        if (!(value instanceof JsonArray array)) throw new IllegalArgumentException("Manifest array is missing: " + name);
        return array;
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
