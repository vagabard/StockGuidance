package stockguidance.database;

public final class DatabaseAccessException extends RuntimeException {
    public DatabaseAccessException(String message) {
        super(message);
    }
}
