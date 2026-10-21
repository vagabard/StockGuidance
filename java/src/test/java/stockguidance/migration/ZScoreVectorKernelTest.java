package stockguidance.migration;

import org.junit.jupiter.api.Test;
import java.util.Random;
import static org.junit.jupiter.api.Assertions.assertEquals;

class ZScoreVectorKernelTest {
    @Test
    void matchesIndependentTwoPassCalculationAcrossSimdAndBatchTails() {
        Random generator = new Random(9187);
        double[][] values = new double[61][1003];
        for (double[] session : values) {
            for (int ticker = 0; ticker < session.length; ticker++) session[ticker] = 10 + generator.nextDouble() * 190;
        }
        for (int window : new int[]{20, 60}) {
            double[] actual = ZScoreVectorKernel.scores(values, 61 - window, 61);
            for (int ticker = 0; ticker < actual.length; ticker++) {
                double mean = 0;
                for (int session = 61 - window; session < 61; session++) mean += values[session][ticker] / window;
                double variance = 0;
                for (int session = 61 - window; session < 61; session++) variance += Math.pow(values[session][ticker] - mean, 2) / window;
                assertEquals((values[60][ticker] - mean) / Math.sqrt(variance), actual[ticker], 1e-12);
            }
        }
    }
}
