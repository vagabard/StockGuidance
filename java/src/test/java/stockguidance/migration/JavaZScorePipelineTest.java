package stockguidance.migration;

import org.junit.jupiter.api.Test;
import java.sql.SQLException;
import java.util.concurrent.atomic.AtomicInteger;
import static org.junit.jupiter.api.Assertions.*;

class JavaZScorePipelineTest {
    @Test
    void tuningChoosesSmallestWorkerCountCloseToBestMeasuredMedian() {
        assertEquals(4, JavaZScorePipelineMain.selectWorkers(java.util.Map.of(1, 10.0, 2, 8.0, 4, 5.1, 8, 5.0, 10, 5.05)));
    }
    @Test
    void retriesOnlyRetryablePublicationFailuresWithABoundedAttemptCount() throws Exception {
        AtomicInteger attempts = new AtomicInteger();
        String result = JavaZScorePipelineMain.retryPublication(() -> {
            if (attempts.incrementAndGet() < 3) throw new SQLException("deadlock", "40001", 1213);
            return "committed";
        });
        assertEquals("committed", result);
        assertEquals(3, attempts.get());
        attempts.set(0);
        assertThrows(SQLException.class, () -> JavaZScorePipelineMain.retryPublication(() -> {
            attempts.incrementAndGet();
            throw new SQLException("lock timeout", "HY000", 1205);
        }));
        assertEquals(3, attempts.get());
        attempts.set(0);
        assertThrows(SQLException.class, () -> JavaZScorePipelineMain.retryPublication(() -> {
            attempts.incrementAndGet();
            throw new SQLException("connection lost", "08006", 2013);
        }));
        assertEquals(1, attempts.get());
    }
}
