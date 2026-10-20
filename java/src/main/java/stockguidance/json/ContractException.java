package stockguidance.json;

public final class ContractException extends RuntimeException {
    public ContractException(String message) {
        super(message);
    }

    public ContractException(String message, Throwable cause) {
        super(message, cause);
    }
}
