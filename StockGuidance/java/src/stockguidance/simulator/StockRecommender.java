package stockguidance.simulator;

import stockguidance.models.SingleRun;
import java.io.File;
import java.util.List;
import java.util.Map;

public abstract class StockRecommender {
    protected final SingleRun singleRun;

    public StockRecommender(SingleRun singleRun) {
        this.singleRun = singleRun;
    }

    public abstract String getOutputDirectory();

    public abstract Recommendations getBuyRecommendations(
            int topK,
            int currentDateId,
            String dbinfoFile,
            float[] precalculatedScores,
            Map<Integer, Float> precalculatedTransactions
    );

    public int getOutputSize() {
        return singleRun.outputSize != null ? singleRun.outputSize : 0;
    }

    public List<Integer> getTickerIds() {
        return singleRun.tickerIds;
    }

    public static class Recommendations {
        public final float[] scores;
        public final List<Integer> topScoringTickerIds;

        public Recommendations(float[] scores, List<Integer> topScoringTickerIds) {
            this.scores = scores;
            this.topScoringTickerIds = topScoringTickerIds;
        }
    }
}
