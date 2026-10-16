package stockguidance.database;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.io.IOException;
import java.nio.file.Path;
import java.nio.file.Paths;

public class DbConnect {

    public static Connection getConnection(DbInfo dbInfo, String dbinfoFile) throws SQLException, IOException {
        DbInfo info;
        if (dbInfo != null) {
            info = dbInfo.withEnvDefaults();
        } else {
            Path foundPath = findDbInfoFile(dbinfoFile);
            if (foundPath == null) {
                throw new IOException("dbinfo_file not found: " + dbinfoFile);
            }
            DbInfo loaded = DbInfo.load(foundPath.toString());
            if (loaded == null) {
                throw new IOException("Database info file not found or invalid: " + foundPath);
            }
            info = loaded.withEnvDefaults();
        }

        String url = String.format("jdbc:mysql://%s:%d/%s", info.getHost(), info.getPort(), info.getSchema());
        // Note: In a real production environment, you might want to use a connection pool.
        return DriverManager.getConnection(url, info.getUser(), info.getPassword());
    }

    private static Path findDbInfoFile(String dbinfoFile) {
        Path current = Paths.get("").toAbsolutePath();
        while (current != null) {
            Path candidate = current.resolve(dbinfoFile);
            if (candidate.toFile().exists()) {
                return candidate;
            }
            current = current.getParent();
        }
        return null;
    }
}
