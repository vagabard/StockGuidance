package stockguidance.migration;

import org.junit.jupiter.api.Test;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

class JavaDbCopyTest {
    @Test
    void overlappingConcurrentRequestsWaitForOneBatchAndReuseImmutableRows() throws Exception {
        LocalDate first = LocalDate.of(2024, 1, 2);
        LocalDate second = first.plusDays(1);
        CountDownLatch loading = new CountDownLatch(1);
        CountDownLatch release = new CountDownLatch(1);
        AtomicInteger calls = new AtomicInteger();
        try (JavaDbCopy source = new JavaDbCopy(Map.of(1L, "AAP", 2L, "AAp"), dates -> {
            calls.incrementAndGet();
            loading.countDown();
            if (!release.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("Test timed out");
            return sessions(dates);
        }); var callers = Executors.newFixedThreadPool(8)) {
            CompletableFuture<Map<LocalDate, JavaDbCopy.SessionData>> initial = source.request(List.of(first, second));
            assertTrue(loading.await(5, TimeUnit.SECONDS));
            List<java.util.concurrent.Future<CompletableFuture<Map<LocalDate, JavaDbCopy.SessionData>>>> requests = new ArrayList<>();
            for (int index = 0; index < 8; index++) requests.add(callers.submit(() -> source.request(List.of(second))));
            for (var request : requests) assertFalse(request.get().isDone());
            release.countDown();
            var original = initial.get(5, TimeUnit.SECONDS);
            for (var request : requests) assertSame(original.get(second), request.get().get().get(second));
            assertSame(original.get(first), source.get(List.of(first)).get(first));
            assertThrows(UnsupportedOperationException.class, () -> original.get(first).raw().clear());
            assertEquals(1, calls.get());
            assertEquals(2, source.sessionCount());
            assertEquals(1, source.batchCount());
        } finally {
            release.countDown();
        }
    }

    @Test
    void batchesAreBoundedAndEmptySessionsAreCachedWithoutQueriesOnRepeatedRequests() throws Exception {
        Map<LocalDate, Integer> counts = new HashMap<>();
        List<LocalDate> dates = new ArrayList<>();
        for (int index = 0; index < 45; index++) dates.add(LocalDate.of(2024, 1, 2).plusDays(index));
        try (JavaDbCopy source = new JavaDbCopy(Map.of(1L, "AAP"), batch -> {
            assertTrue(batch.size() <= 20);
            Map<LocalDate, JavaDbCopy.SessionData> result = new HashMap<>();
            for (LocalDate date : batch) {
                counts.merge(date, 1, Integer::sum);
                result.put(date, new JavaDbCopy.SessionData(null, null, Map.of(), Map.of()));
            }
            return result;
        })) {
            assertEquals(45, source.get(dates).size());
            assertEquals(45, source.get(dates).size());
            assertEquals(3, source.batchCount());
            assertEquals(45, counts.size());
            assertTrue(counts.values().stream().allMatch(count -> count == 1));
        }
    }

    @Test
    void failedBatchReleasesEveryWaiterAndDoesNotSilentlyRetryOrPublishPartialData() throws Exception {
        LocalDate date = LocalDate.of(2024, 1, 2);
        AtomicInteger calls = new AtomicInteger();
        try (JavaDbCopy source = new JavaDbCopy(Map.of(1L, "AAP"), dates -> {
            calls.incrementAndGet();
            throw new IllegalArgumentException("Changed archive identity");
        })) {
            assertThrows(IllegalStateException.class, () -> source.get(List.of(date)));
            assertThrows(IllegalStateException.class, () -> source.get(List.of(date)));
            assertEquals(1, calls.get());
        }
    }

    @Test
    void recordConstructionDoesNotExposeMutableBackingMapsOrLoseNullValues() {
        Map<Long, JavaDbCopy.RawRow> raw = new HashMap<>();
        raw.put(1L, new JavaDbCopy.RawRow(1, 2, null));
        var session = new JavaDbCopy.SessionData(2L, null, raw, Map.of());
        raw.clear();
        assertEquals(1, session.raw().size());
        assertNull(session.raw().get(1L).close());
        assertThrows(UnsupportedOperationException.class, () -> session.raw().put(2L, new JavaDbCopy.RawRow(2, 2, 1.0)));
    }

    private static Map<LocalDate, JavaDbCopy.SessionData> sessions(List<LocalDate> dates) {
        Map<LocalDate, JavaDbCopy.SessionData> result = new HashMap<>();
        for (LocalDate date : dates) result.put(date, new JavaDbCopy.SessionData(2L, null,
                Map.of(1L, new JavaDbCopy.RawRow(1, 2, 123.5)), Map.of()));
        return result;
    }
}
