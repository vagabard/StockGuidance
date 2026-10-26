package stockguidance.migration;

/**
 * Historical entrypoint retained only so stale launch configurations fail
 * safely after the Stage 8 cutover. Supported maintenance is owned by the
 * Python simplified-schema coordinator; Java numerical contracts remain
 * available through the shared database package and disposable writer tools.
 */
public final class JavaDailyMaintenanceMain {
    private JavaDailyMaintenanceMain() {
    }

    public static void main(String[] arguments) {
        System.err.println("Java daily maintenance writer is retired; use the Python simplified-schema coordinator");
        System.out.println("{\"failed\":true,\"status\":\"retired\",\"error\":\"Use the Python simplified-schema coordinator; legacy Java maintenance cannot write this database\"}");
        System.exit(2);
    }
}
