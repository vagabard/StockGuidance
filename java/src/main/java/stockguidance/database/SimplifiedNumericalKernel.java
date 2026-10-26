package stockguidance.database;

import stockguidance.validation.LegacyDerivedCalculator;
import stockguidance.validation.ValidationContent;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static stockguidance.database.SimplifiedContracts.CalculationStatus;
import static stockguidance.database.SimplifiedContracts.DataState;

/**
 * Java's target-schema numerical value boundary.
 *
 * The legacy formula kernel remains the authority for the sixteen derived
 * columns.  This adapter adds target-schema state and provenance without
 * creating another formula implementation.
 */
public final class SimplifiedNumericalKernel {
    private static final int DERIVED_COLUMN_COUNT = 16;

    private SimplifiedNumericalKernel() {
    }

    public record RawObservation(long tickerId, String label, ValidationContent.RawBar bar,
                                 DataState state, boolean available, int qualityFlags, int gapLength,
                                 List<String> provenance, Instant availableAt) {
        public RawObservation {
            if (tickerId <= 0 || label == null || label.isBlank() || state == null) {
                throw new IllegalArgumentException("Invalid numerical raw observation");
            }
            if (qualityFlags < 0 || gapLength < 0 || provenance == null) {
                throw new IllegalArgumentException("Invalid numerical quality evidence");
            }
            provenance = List.copyOf(provenance);
        }

        public boolean usable() {
            return bar != null && state != DataState.BAD && state != DataState.MISSING && available;
        }
    }

    public record DeltaValue(long tickerId, List<Double> values, CalculationStatus status,
                             int qualityFlags, int gapLength, List<String> provenance) {
        public DeltaValue {
            if (tickerId <= 0 || values == null || values.size() != DERIVED_COLUMN_COUNT || status == null
                    || qualityFlags < 0 || gapLength < 0 || provenance == null) {
                throw new IllegalArgumentException("Invalid target delta value");
            }
            for (Double value : values) {
                if (value != null && !Double.isFinite(value)) {
                    throw new IllegalArgumentException("Derived values must be finite or NULL");
                }
            }
            values = java.util.Collections.unmodifiableList(new ArrayList<>(values));
            provenance = List.copyOf(provenance);
        }
    }

    public static List<DeltaValue> calculateDeltas(List<RawObservation> prior,
                                                    List<RawObservation> current,
                                                    List<RawObservation> following,
                                                    boolean followingSessionExists) {
        Map<String, ValidationContent.RawBar> priorBars = usableBars(prior);
        Map<String, ValidationContent.RawBar> currentBars = usableBars(current);
        Map<String, ValidationContent.RawBar> followingBars = usableBars(following);
        Map<String, List<Double>> expected = LegacyDerivedCalculator.expected(priorBars, currentBars, followingBars);
        Set<String> followingPopulation = new HashSet<>(followingBars.keySet());
        Map<Long, RawObservation> priorByTicker = byTicker(prior);
        Map<Long, RawObservation> followingByTicker = byTicker(following);
        List<DeltaValue> result = new ArrayList<>();
        current.stream().sorted(Comparator.comparingLong(RawObservation::tickerId)).forEach(observation -> {
            CalculationStatus status = sourceStatus(observation);
            if (status == CalculationStatus.READY) {
                RawObservation previous = priorByTicker.get(observation.tickerId());
                status = higher(status, previous == null ? CalculationStatus.INSUFFICIENT_HISTORY : sourceStatus(previous));
                RawObservation next = followingByTicker.get(observation.tickerId());
                status = higher(status, !followingSessionExists ? CalculationStatus.NOT_YET_AVAILABLE
                        : next == null ? CalculationStatus.MISSING_INPUT : sourceStatus(next));
            }
            List<Double> values = sourceStatus(observation) == CalculationStatus.READY
                    ? expected.getOrDefault(observation.label(), java.util.Collections.nCopies(DERIVED_COLUMN_COUNT, null))
                    : java.util.Collections.nCopies(DERIVED_COLUMN_COUNT, null);
            if (values.size() != DERIVED_COLUMN_COUNT) {
                values = java.util.Collections.nCopies(DERIVED_COLUMN_COUNT, null);
            }
            int qualityFlags = observation.qualityFlags();
            int gapLength = observation.gapLength();
            Set<String> provenance = new HashSet<>(observation.provenance());
            RawObservation previous = priorByTicker.get(observation.tickerId());
            RawObservation next = followingByTicker.get(observation.tickerId());
            for (RawObservation item : java.util.Arrays.asList(previous, next)) {
                if (item != null) {
                    qualityFlags |= item.qualityFlags();
                    gapLength = Math.max(gapLength, item.gapLength());
                    provenance.addAll(item.provenance());
                }
            }
            for (RawObservation item : following) {
                if (followingPopulation.contains(item.label())) {
                    qualityFlags |= item.qualityFlags();
                    gapLength = Math.max(gapLength, item.gapLength());
                    provenance.addAll(item.provenance());
                }
            }
            result.add(new DeltaValue(observation.tickerId(), values, status, qualityFlags, gapLength,
                    provenance.stream().sorted().toList()));
        });
        return List.copyOf(result);
    }

    private static Map<String, ValidationContent.RawBar> usableBars(List<RawObservation> values) {
        Map<String, ValidationContent.RawBar> result = new HashMap<>();
        for (RawObservation value : values) {
            if (value.usable()) {
                result.put(value.label(), value.bar());
            }
        }
        return result;
    }

    private static Map<Long, RawObservation> byTicker(List<RawObservation> values) {
        Map<Long, RawObservation> result = new HashMap<>();
        for (RawObservation value : values) {
            if (result.put(value.tickerId(), value) != null) {
                throw new IllegalArgumentException("Duplicate numerical ticker ID");
            }
        }
        return result;
    }

    private static CalculationStatus sourceStatus(RawObservation value) {
        if (value.state() == DataState.BAD) return CalculationStatus.INVALID_INPUT;
        if (value.state() == DataState.MISSING) return CalculationStatus.MISSING_INPUT;
        return value.available() ? CalculationStatus.READY : CalculationStatus.NOT_YET_AVAILABLE;
    }

    private static CalculationStatus higher(CalculationStatus first, CalculationStatus second) {
        return rank(first) >= rank(second) ? first : second;
    }

    private static int rank(CalculationStatus value) {
        return switch (value) {
            case READY -> 0;
            case INSUFFICIENT_HISTORY -> 1;
            case MISSING_INPUT -> 2;
            case NOT_YET_AVAILABLE -> 3;
            case INVALID_INPUT -> 4;
            case ZERO_VARIANCE -> 5;
        };
    }
}
