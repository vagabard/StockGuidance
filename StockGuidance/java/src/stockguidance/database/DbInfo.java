package stockguidance.database;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.HashMap;
import java.util.Map;
import java.util.Optional;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class DbInfo {
    private String host = "localhost";
    private String database = "stockforecasting";
    private String user;
    private String password;
    private int port = 3306;
    private String driver = "auto";
    private String schema;

    public DbInfo() {}

    public DbInfo(String host, String database, String user, String password, int port, String driver, String schema) {
        this.host = host;
        this.database = database;
        this.user = user;
        this.password = password;
        this.port = port;
        this.driver = driver;
        this.schema = schema;
    }

    public String getHost() { return host; }
    public void setHost(String host) { this.host = host; }
    public String getDatabase() { return database; }
    public void setDatabase(String database) { this.database = database; }
    public String getUser() { return user; }
    public void setUser(String user) { this.user = user; }
    public String getPassword() { return password; }
    public void setPassword(String password) { this.password = password; }
    public int getPort() { return port; }
    public void setPort(int port) { this.port = port; }
    public String getDriver() { return driver; }
    public void setDriver(String driver) { this.driver = driver; }
    public String getSchema() { return schema != null ? schema : database; }
    public void setSchema(String schema) { this.schema = schema; }

    public DbInfo withEnvDefaults() {
        String envUser = Optional.ofNullable(System.getenv("MYSQL_USER")).orElse(System.getenv("DB_USER"));
        String envPass = Optional.ofNullable(System.getenv("MYSQL_PASSWORD")).orElse(System.getenv("DB_PASSWORD"));
        String envPort = System.getenv("MYSQL_PORT");
        String envHost = System.getenv("MYSQL_HOST");
        String envDatabase = System.getenv("MYSQL_DATABASE");
        String envDriver = System.getenv("MYSQL_DRIVER");

        return new DbInfo(
            host != null ? host : (envHost != null ? envHost : "localhost"),
            database != null ? database : (envDatabase != null ? envDatabase : "stockforecasting"),
            user != null ? user : envUser,
            password != null ? password : (envPass != null ? envPass : ""),
            envPort != null ? Integer.parseInt(envPort) : port,
            driver != null ? driver : (envDriver != null ? envDriver : "auto"),
            schema != null ? schema : (database != null ? database : "stockforecasting")
        );
    }

    public static DbInfo load(String filePath) throws IOException {
        Path path = Paths.get(filePath);
        if (!Files.exists(path)) return null;

        String content = new String(Files.readAllBytes(path)).trim();
        if (content.isEmpty()) return null;

        if (content.startsWith("{")) {
            // Try parse as JSON (minimal manual parser)
            return parseJson(content);
        } else {
            // Parse as KEY=VALUE
            return parseKeyValue(content);
        }
    }

    private static DbInfo parseJson(String json) {
        DbInfo info = new DbInfo();
        info.setHost(extractJsonString(json, "host", "localhost"));
        info.setDatabase(extractJsonString(json, "database", extractJsonString(json, "db", "stockforecasting")));
        info.setUser(extractJsonString(json, "user", null));
        info.setPassword(extractJsonString(json, "password", null));
        info.setPort(extractJsonInt(json, "port", 3306));
        info.setDriver(extractJsonString(json, "driver", "auto"));
        info.setSchema(extractJsonString(json, "schema", info.getDatabase()));
        return info;
    }

    private static String extractJsonString(String json, String key, String defaultValue) {
        Pattern p = Pattern.compile("\"" + key + "\":\\s*\"?([^\",\\s}]+)\"?");
        Matcher m = p.matcher(json);
        if (m.find()) {
            return m.group(1);
        }
        return defaultValue;
    }

    private static int extractJsonInt(String json, String key, int defaultValue) {
        Pattern p = Pattern.compile("\"" + key + "\":\\s*(\\d+)");
        Matcher m = p.matcher(json);
        if (m.find()) {
            return Integer.parseInt(m.group(1));
        }
        return defaultValue;
    }

    private static DbInfo parseKeyValue(String content) {
        Map<String, String> data = new HashMap<>();
        for (String line : content.split("\\r?\\n")) {
            line = line.trim();
            if (line.isEmpty() || line.startsWith("#") || line.startsWith("//") || line.startsWith(";")) continue;
            if (line.contains("=")) {
                String[] parts = line.split("=", 2);
                data.put(parts[0].trim().toLowerCase(), parts[1].trim());
            }
        }
        if (data.isEmpty()) return null;

        DbInfo info = new DbInfo();
        info.setHost(data.getOrDefault("host", "localhost"));
        info.setDatabase(data.getOrDefault("database", data.getOrDefault("db", data.getOrDefault("schema", "stockforecasting"))));
        info.setUser(data.getOrDefault("user", data.get("username")));
        info.setPassword(data.getOrDefault("password", data.getOrDefault("pass", data.get("pwd"))));
        info.setPort(Integer.parseInt(data.getOrDefault("port", "3306")));
        info.setDriver(data.getOrDefault("driver", "auto"));
        info.setSchema(data.getOrDefault("schema", info.getDatabase()));
        return info;
    }

    @Override
    public String toString() {
        return "DbInfo{" +
                "host='" + host + '\'' +
                ", database='" + database + '\'' +
                ", user='" + user + '\'' +
                ", port=" + port +
                ", driver='" + driver + '\'' +
                ", schema='" + getSchema() + '\'' +
                '}';
    }
}
