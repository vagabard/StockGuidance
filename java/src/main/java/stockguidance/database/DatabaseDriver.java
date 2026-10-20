package stockguidance.database;

public enum DatabaseDriver {
    AUTO("auto"),
    CONNECTOR("mysqlconnector"),
    PYMYSQL("pymysql");

    private final String value;

    DatabaseDriver(String value) {
        this.value = value;
    }

    public String value() {
        return value;
    }

    public static DatabaseDriver parse(String value) {
        for (DatabaseDriver driver : values()) {
            if (driver.value.equals(value)) {
                return driver;
            }
        }
        throw new IllegalArgumentException("Unsupported database driver");
    }
}
