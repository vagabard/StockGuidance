package stockguidance.migration;

import stockguidance.database.DatabaseConfiguration;
import stockguidance.database.MySqlConnectionFactory;
import stockguidance.database.SqlIdentifiers;
import stockguidance.database.TableNames;
import stockguidance.json.CanonicalJson;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.util.List;
import java.util.Map;

public final class JavaRawBenchmarkDebugMain {
    private static final int PREPARERS = 3;
    private static final String DISPOSABLE_SCHEMA = "stockguidance_disposable_s9a_j3_e61bd980";
    private static final String SESSIONS = String.join(",",
            "2006-12-08", "2007-09-11", "2008-05-07", "2008-08-04", "2009-10-14",
            "2010-05-10", "2010-10-04", "2011-11-03", "2012-10-17", "2012-12-24",
            "2013-11-15", "2014-12-04", "2015-08-06", "2017-03-13", "2018-08-02",
            "2018-08-29", "2020-03-09", "2020-03-12", "2021-10-04", "2021-12-09",
            "2022-02-11", "2023-02-17", "2024-01-31", "2024-10-14", "2026-04-29");

    private JavaRawBenchmarkDebugMain() {
    }

    public static void main(String[] ignoredArguments) throws Exception {
        if (ignoredArguments.length != 0) {
            throw new IllegalArgumentException("Run this entrypoint without command-line arguments");
		}
		try {
			Path repository = repositoryRoot();
			Path manifestPath = repository.resolve("output/legacy-migration/benchmark/candidates/manifest.json");
			Path dbinfoPath = repository.resolve("dbinfo.id");
			Path output = repository.resolve("output/legacy-migration/benchmark/debug-java-p3");
			for (int i = 0; i < 3; i++){
				resetRawTables(manifestPath, dbinfoPath);
				Map<String, String> benchmarkArguments = Map.of(
						"manifest", manifestPath.toString(),
						"dbinfo-file", dbinfoPath.toString(),
						"disposable-schema", DISPOSABLE_SCHEMA,
						"preparers", Integer.toString(PREPARERS),
						"sessions", SESSIONS,
						"checkpoint-directory", output.resolve("checkpoints").toString());

				System.err.println("Starting JavaRawBenchmarkMain with " + PREPARERS + " preparers");
				System.err.println("Repository: " + repository);
				System.err.println("Schema: " + DISPOSABLE_SCHEMA);
				System.err.println("Sessions: " + SESSIONS);
				System.err.println("Breakpoints can be set in JavaRawBenchmarkMain and JavaWriterCandidate");
				String report = new String(CanonicalJson.bytes(stockguidance.migration.JavaRawBenchmarkMain.run(benchmarkArguments)), StandardCharsets.UTF_8);
				System.out.print(report);
			}

		} catch (Exception e) {
			if (e.getCause()!= null){
				e.getCause().printStackTrace();
			}
			e.printStackTrace();
			throw e;
		}
	}

	private static void resetRawTables(Path manifestPath, Path dbinfoPath) throws SQLException {
		JavaWriterManifest manifest = JavaWriterManifest.read(manifestPath);
		DatabaseConfiguration configured = DatabaseConfiguration.load(dbinfoPath);
		DatabaseConfiguration targetConfiguration = new DatabaseConfiguration(
				configured.host(), DISPOSABLE_SCHEMA, configured.user(), configured.password(),
				configured.port(), configured.driver(), configured.tableNamesFile());
		if (!targetConfiguration.loadTableNames().tables().equals(manifest.tableNames().tables())) {
			throw new IllegalArgumentException("Disposable database registry does not match the manifest");
		}
		TableNames names = manifest.tableNames();
		try (Connection connection = new MySqlConnectionFactory().open(targetConfiguration)) {
			new JavaWriterCandidate(manifest).validateTarget(connection);
			boolean originalAutoCommit = connection.getAutoCommit();
			try {
				connection.setAutoCommit(false);
				for (String table : List.of("stage6_completion", names.imports(), names.deltas(), names.prices())) {
					String sql = "DELETE FROM " + SqlIdentifiers.quote(table, table);
					try (PreparedStatement statement = connection.prepareStatement(sql)) {
						int deletedRows = statement.executeUpdate();
						System.err.println("Reset " + table + ": " + deletedRows + " rows");
					}
				}
				connection.commit();
			} catch (RuntimeException | SQLException error) {
				try {
					connection.rollback();
				} catch (SQLException ignored) {
				}
				throw error;
			} finally {
				try {
					connection.setAutoCommit(originalAutoCommit);
				} catch (SQLException ignored) {
				}
			}
		}
	}

	private static Path repositoryRoot() {
        Path workingDirectory = Path.of("").toAbsolutePath().normalize();
        if (Files.isRegularFile(workingDirectory.resolve("output/legacy-migration/benchmark/candidates/manifest.json"))) {
            return workingDirectory;
        }
        Path parent = workingDirectory.getParent();
        if (parent != null && Files.isRegularFile(parent.resolve("output/legacy-migration/benchmark/candidates/manifest.json"))) {
            return parent;
        }
        throw new IllegalStateException("Run from the repository root or its java directory");
    }
}
