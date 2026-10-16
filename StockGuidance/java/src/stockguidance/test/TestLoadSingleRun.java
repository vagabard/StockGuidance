package stockguidance.test;

import stockguidance.models.SingleRun;
import java.io.File;
import java.util.List;

public class TestLoadSingleRun {
    public static void main(String[] args) throws Exception {
        File file = new File("data/run_family_20260304_183343/run_single_20260304_183343_00/single_run_0.json");
        SingleRun sr = SingleRun.load(file);
        System.out.println("SingleRun: " + sr);
        List<Integer> tickerIds = sr.tickerIds;
        if (tickerIds == null) {
            System.out.println("Ticker IDs are NULL");
        } else {
            System.out.println("Ticker IDs count: " + tickerIds.size());
            if (!tickerIds.isEmpty()) {
                System.out.println("First few: " + tickerIds.subList(0, Math.min(10, tickerIds.size())));
            }
        }
    }
}
