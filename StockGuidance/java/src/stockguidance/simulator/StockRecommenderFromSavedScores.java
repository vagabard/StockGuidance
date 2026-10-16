package stockguidance.simulator;

import stockguidance.models.SingleRun;
import java.util.*;

public class StockRecommenderFromSavedScores extends StockRecommender {
    private final String outputDirectory;

    public StockRecommenderFromSavedScores(SingleRun singleRun, String outputDirectory) {
        super(singleRun);
        this.outputDirectory = outputDirectory;
    }

    @Override
    public String getOutputDirectory() {
        return outputDirectory;
    }

    @Override
    public Recommendations getBuyRecommendations(
            int topK,
            int currentDateId,
            String dbinfoFile,
            float[] precalculatedScores,
            Map<Integer, Float> precalculatedTransactions
    ) {
        if (precalculatedScores == null) {
            throw new IllegalArgumentException("precalculatedScores must be provided to StockRecommenderFromSavedScores");
        }

        int outputSize = getOutputSize();
        if (outputSize == 0) {
            outputSize = precalculatedScores.length;
        }

        int finalTopK = topK;
        if (finalTopK <= 0) finalTopK = 1;

        // In Python, it filters by transactions if precalculated_transactions is provided.
        // MIN_TRADES_FOR_RECOMMENDATION = 200
        int MIN_TRADES = 200;

        float[] scoresWithFilter = precalculatedScores.clone();
        List<Integer> tickerIds = getTickerIds();

        if (precalculatedTransactions != null && tickerIds != null) {
            for (int i = 0; i < scoresWithFilter.length; i++) {
                int tickerId = tickerIds.get(i);
                Float trans = precalculatedTransactions.get(tickerId);
                if (trans == null || trans < MIN_TRADES) {
                    scoresWithFilter[i] = -1e9f; // Low score for filtered out tickers
                }
            }
        }

        // Apply mask
        float[] maskedScores = scoresWithFilter.clone();
        for (int i = 0; i < maskedScores.length; i++) {
            if (maskedScores[i] <= -1e8f) {
                maskedScores[i] = Float.NEGATIVE_INFINITY;
            }
        }

        // Get top K indices
        Integer[] indices = new Integer[maskedScores.length];
        for (int i = 0; i < indices.length; i++) indices[i] = i;

        Arrays.sort(indices, (a, b) -> {
            float sa = maskedScores[a];
            float sb = maskedScores[b];
            if (sa == sb) return Integer.compare(a, b);
            return Float.compare(sb, sa);
        });

        List<Integer> topScoringTickerIds = new ArrayList<>();
        for (int i = 0; i < indices.length; i++) {
            int idx = indices[i];
            if (maskedScores[idx] == Float.NEGATIVE_INFINITY) break;

            // In Python's simulate_trading.py, it uses the 1-based index in the scores file as the recommendation ID.
            // This is because the scores are already aligned with the ticker pool used during training.
            topScoringTickerIds.add(idx + 1);
        }

        return new Recommendations(maskedScores, topScoringTickerIds);
    }
}
