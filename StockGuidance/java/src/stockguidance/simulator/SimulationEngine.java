package stockguidance.simulator;

import stockguidance.database.DbConnect;
import stockguidance.database.DbInfo;
import stockguidance.models.SingleRun;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.Locale;

public class SimulationEngine {
    private final List<StockRecommender> recommenders;
    private final List<Integer> topK;
    private final String dbinfoFile;
    private final boolean onlyEnsemble;
    private final boolean force;
    private final String javaTag = "_java";

    public SimulationEngine(List<StockRecommender> recommenders, List<Integer> topK, String dbinfoFile, boolean onlyEnsemble, boolean force) {
        this.recommenders = recommenders;
        this.topK = topK;
        this.dbinfoFile = dbinfoFile;
        this.onlyEnsemble = onlyEnsemble;
        this.force = force;
    }

    public List<String> runSimulation(String epochName, List<Integer> datesToHandle) throws Exception {
        if (recommenders.isEmpty()) return Collections.emptyList();

        StockRecommender mainRec = recommenders.get(0);
        int numDays = datesToHandle.size();
        String simulationSize = numDays + "days";

        List<SimulationTask> tasks = identifyTasks(mainRec, simulationSize, epochName);
        if (tasks.isEmpty()) return Collections.emptyList();

        PrefetchedData data = prefetchData(datesToHandle);

        Map<String, Double> portfolioValues = new HashMap<>(); // (method+k) -> value

        for (SimulationTask task : tasks) {
            if (task.outPath.exists() && !force) {
                System.out.println("Skipping existing simulation: " + task.outPath.getAbsolutePath());
                continue;
            }

            System.out.println("Running simulation for: " + task.outPath.getAbsolutePath());
            try (PrintWriter writer = new PrintWriter(new BufferedWriter(new FileWriter(task.outPath)))) {
                writeHeader(writer, task.sourceRecommenders.size() > 1);

                for (int i = 0; i < datesToHandle.size(); i++) {
                    int featureDateId = datesToHandle.get(i);
                    int tradeDateId = -1;
                    if (i + 1 < datesToHandle.size()) {
                        tradeDateId = datesToHandle.get(i + 1);
                    } else {
                        // Find next date from DB or provider?
                        // Simplification: we need the date AFTER featureDateId in the chronological sequence.
                        // For now, if we don't have it in datesToHandle, we might need to fetch it.
                        tradeDateId = data.getNextDate(featureDateId);
                    }

                    if (tradeDateId == -1) continue;

                    Map<String, float[]> aggregatedScores = loadAndAggregateScores(task.sourceRecommenders, featureDateId, epochName);
                    if (aggregatedScores == null) continue;

                    Map<Integer, Float> transactions = data.transactions.getOrDefault(featureDateId, Collections.emptyMap());
                    Map<String, DailyResult> methodResults = new HashMap<>();

                    for (Map.Entry<String, float[]> entry : aggregatedScores.entrySet()) {
                        String methodName = entry.getKey();
                        float[] scores = entry.getValue();

                        int maxK = 0;
                        for (int k : topK) if (k > maxK) maxK = k;

                        StockRecommender.Recommendations recs = task.mainRecommender.getBuyRecommendations(
                                maxK, featureDateId, dbinfoFile, scores, transactions);

                        Map<Integer, DailyResult.KResult> kResults = new HashMap<>();
                        for (int k : topK) {
                            List<Integer> chosen = recs.topScoringTickerIds.subList(0, Math.min(k, recs.topScoringTickerIds.size()));

                            double dayRatioAvg = calculateDayRatioAvg(chosen, data.priceData.get(tradeDateId));
                            double rank = calculatePercentileRank(chosen, data.allRatios.get(tradeDateId), task.mainRecommender.getTickerIds());

                            String portKey = methodName + "_" + k + "_" + task.outPath.getAbsolutePath();
                            double portVal = portfolioValues.getOrDefault(portKey, 1000.0);
                            portVal *= dayRatioAvg;
                            portfolioValues.put(portKey, portVal);

                            kResults.put(k, new DailyResult.KResult(dayRatioAvg - 1.0, rank, portVal));
                        }

                        int maxKVal = 0;
                        for (int kVal : topK) if (kVal > maxKVal) maxKVal = kVal;

                        String topKRecommendedStr = recs.topScoringTickerIds.stream()
                            .limit(maxKVal)
                            .map(Object::toString)
                            .collect(Collectors.joining("|"));

                        methodResults.put(methodName, new DailyResult(kResults, topKRecommendedStr));
                    }

                    writeRow(writer, featureDateId, tradeDateId, methodResults, task.sourceRecommenders.size() > 1);
                }
            }
        }

        return tasks.stream().map(t -> t.outPath.getAbsolutePath()).collect(Collectors.toList());
    }

    private List<SimulationTask> identifyTasks(StockRecommender mainRec, String simulationSize, String epochName) {
        List<SimulationTask> tasks = new ArrayList<>();
        String suffix = "_epoch_" + epochName + javaTag;
        int numNets = recommenders.size();

        if (numNets > 1) {
            // Ensemble task
            String targetDir = mainRec.getOutputDirectory();
            String outName = "stock_simulation_ensemble_" + simulationSize + suffix + ".csv";
            tasks.add(new SimulationTask(new File(targetDir, outName), recommenders.subList(1, recommenders.size()), mainRec));

            // Individual rep tasks
            Set<String> seenDirs = new HashSet<>();
            for (int i = 1; i < recommenders.size(); i++) {
                StockRecommender rep = recommenders.get(i);
                String repDir = rep.getOutputDirectory();
                if (seenDirs.contains(repDir)) continue;
                seenDirs.add(repDir);
                String repOutName = "stock_simulation_" + simulationSize + suffix + ".csv";
                tasks.add(new SimulationTask(new File(repDir, repOutName), Collections.singletonList(rep), rep));
            }
        } else {
            String outName = "stock_simulation_" + simulationSize + suffix + ".csv";
            tasks.add(new SimulationTask(new File(mainRec.getOutputDirectory(), outName), recommenders, mainRec));
        }
        return tasks;
    }

    private void writeHeader(PrintWriter writer, boolean isEnsemble) {
        List<String> cols = new ArrayList<>(Arrays.asList("feature_date_id", "open_date_id", "close_date_id", "topNRecommends"));
        String[] methods = isEnsemble ? new String[]{"Sum", "Norm", "Rank"} : new String[]{""};
        for (int i = 0; i < methods.length; i++) {
            String method = methods[i];
            String suffix = method.isEmpty() ? "" : " (" + method + ")";
            for (int k : topK) {
                cols.add("Return Percent top" + k + suffix);
                cols.add("Rank Percent top" + k + suffix);
                cols.add("Portfolio return top" + k + suffix);
            }
        }
        writer.println(String.join(",", cols));
    }

    private void writeRow(PrintWriter writer, int featureDateId, int tradeDateId, Map<String, DailyResult> results, boolean isEnsemble) {
        List<String> vals = new ArrayList<>();
        vals.add(String.valueOf(featureDateId));
        vals.add(String.valueOf(tradeDateId));
        vals.add(String.valueOf(tradeDateId));

        String primaryMethod = isEnsemble ? "Sum" : "";
        DailyResult primaryRes = results.get(primaryMethod);

        // Ensure we always have some string here, even if null.
        // Python format is tid1|tid2|tid3...
        vals.add(primaryRes != null ? primaryRes.topKRecommendedStr : "");

        String[] methods = isEnsemble ? new String[]{"Sum", "Norm", "Rank"} : new String[]{""};
        for (String method : methods) {
            DailyResult res = results.get(method);
            for (int k : topK) {
                DailyResult.KResult kr = (res != null && res.kResults.containsKey(k)) ? res.kResults.get(k) : new DailyResult.KResult(0, 0, 1000);
                vals.add(String.format(Locale.US, "%.6f", kr.ret));
                vals.add(String.format(Locale.US, "%.6f", kr.rank));
                vals.add(String.format(Locale.US, "%.6f", kr.portVal));
            }
        }
        writer.println(String.join(",", vals));
    }

    private double calculateDayRatioAvg(List<Integer> chosen, Map<Integer, float[]> priceData) {
        if (priceData == null) return 1.0;
        double sum = 0;
        int count = 0;
        for (int tid : chosen) {
            float[] prices = priceData.get(tid);
            if (prices != null && prices[0] > 0) {
                sum += prices[1] / prices[0];
                count++;
            }
        }
        return count > 0 ? sum / count : 1.0;
    }

    private double calculatePercentileRank(List<Integer> chosen, Map<Integer, Float> allRatios, List<Integer> poolTickerIds) {
        if (allRatios == null || allRatios.isEmpty() || chosen.isEmpty()) return 0.5;

        // Python's pandas.rank(pct=True) with default 'average' for ties
        // 1. Calculate average ratio of chosen tickers
        double sumChosen = 0;
        int countChosen = 0;
        for (int tid : chosen) {
            Float ratio = allRatios.get(tid);
            if (ratio != null) {
                sumChosen += ratio;
                countChosen++;
            }
        }
        if (countChosen == 0) return 0.5;
        double avgChosen = sumChosen / countChosen;

        // 2. Rank avgChosen among poolTickerIds' ratios
        Collection<Float> poolRatios;
        if (poolTickerIds != null) {
            poolRatios = new ArrayList<>();
            for (int tid : poolTickerIds) {
                Float r = allRatios.get(tid);
                if (r != null) poolRatios.add(r);
            }
        } else {
            poolRatios = allRatios.values();
        }

        if (poolRatios.isEmpty()) return 0.5;

        int countLess = 0;
        int countEqual = 0;
        for (float r : poolRatios) {
            if (r < (float)avgChosen) countLess++;
            else if (r == (float)avgChosen) countEqual++;
        }

        return (countLess + 0.5 * countEqual) / poolRatios.size();
    }

    private Map<String, float[]> loadAndAggregateScores(List<StockRecommender> sourceRecs, int dateId, String epochName) {
        List<float[]> rawScores = new ArrayList<>();
        List<float[]> normScores = new ArrayList<>();
        List<float[]> rankScores = new ArrayList<>();

        for (StockRecommender rec : sourceRecs) {
            float[] scores = loadScores(rec.getOutputDirectory(), dateId, epochName);
            if (scores == null) return null;
            rawScores.add(scores);
            normScores.add(normalize(scores));
            rankScores.add(rank(scores));
        }

        Map<String, float[]> agg = new HashMap<>();
        if (sourceRecs.size() == 1) {
            agg.put("", rawScores.get(0));
        } else {
            agg.put("Sum", sum(rawScores));
            agg.put("Norm", sum(normScores));
            agg.put("Rank", mean(rankScores));
        }
        return agg;
    }

    private float[] loadScores(String dir, int dateId, String epochName) {
        String fileName = "stock_scores_epoch_" + epochName + "_date_" + dateId + "_net_0.csv";
        File file = new File(dir, fileName);
        if (!file.exists()) return null;

        List<Float> scoresList = new ArrayList<>();
        try (BufferedReader br = new BufferedReader(new FileReader(file))) {
            String line;
            while ((line = br.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty() || line.startsWith("score")) continue; // skip header if any
                try {
                    scoresList.add(Float.parseFloat(line));
                } catch (NumberFormatException e) {
                    scoresList.add((float) Double.parseDouble(line));
                }
            }
            float[] scores = new float[scoresList.size()];
            for (int i = 0; i < scoresList.size(); i++) {
                scores[i] = scoresList.get(i);
            }
            return scores;
        } catch (IOException e) {
            return null;
        }
    }

    private float[] normalize(float[] scores) {
        float sum = 0;
        for (float s : scores) sum += Math.abs(s);
        float[] res = new float[scores.length];
        if (sum > 0) {
            for (int i = 0; i < scores.length; i++) res[i] = scores[i] / sum;
        }
        return res;
    }

    private float[] rank(float[] scores) {
        Integer[] indices = new Integer[scores.length];
        for (int i = 0; i < indices.length; i++) indices[i] = i;
        Arrays.sort(indices, (a, b) -> Float.compare(scores[a], scores[b]));
        float[] res = new float[scores.length];
        for (int i = 0; i < indices.length; i++) {
            res[indices[i]] = (float) i / (scores.length - 1);
        }
        return res;
    }

    private float[] sum(List<float[]> list) {
        float[] res = new float[list.get(0).length];
        for (float[] s : list) {
            for (int i = 0; i < s.length; i++) res[i] += s[i];
        }
        return res;
    }

    private float[] mean(List<float[]> list) {
        float[] res = sum(list);
        for (int i = 0; i < res.length; i++) res[i] /= list.size();
        return res;
    }

    private PrefetchedData prefetchData(List<Integer> datesToHandle) throws Exception {
        int minDate = Collections.min(datesToHandle);
        int maxDate = Collections.max(datesToHandle);

        // We need to fetch one extra date after maxDate to calculate the last day's return.
        // For simplicity, let's fetch a wider range.

        PrefetchedData data = new PrefetchedData();
        try (Connection conn = DbConnect.getConnection(null, dbinfoFile)) {
            String sql = "SELECT date_id, ticker_id, open, close, transactions FROM raw_stock_data WHERE date_id >= ? ORDER BY date_id ASC";
            try (PreparedStatement ps = conn.prepareStatement(sql)) {
                ps.setInt(1, minDate);
                try (ResultSet rs = ps.executeQuery()) {
                    while (rs.next()) {
                        int dId = rs.getInt("date_id");
                        int tId = rs.getInt("ticker_id");
                        float open = rs.getFloat("open");
                        float close = rs.getFloat("close");
                        float trans = rs.getFloat("transactions");

                        data.addDate(dId);
                        data.transactions.computeIfAbsent(dId, k -> new HashMap<>()).put(tId, trans);
                        if (open > 0) {
                            data.allRatios.computeIfAbsent(dId, k -> new HashMap<>()).put(tId, close / open);
                            data.priceData.computeIfAbsent(dId, k -> new HashMap<>()).put(tId, new float[]{open, close});
                        }
                    }
                }
            }
        }
        return data;
    }

    private static class SimulationTask {
        final File outPath;
        final List<StockRecommender> sourceRecommenders;
        final StockRecommender mainRecommender;

        SimulationTask(File outPath, List<StockRecommender> sourceRecommenders, StockRecommender mainRecommender) {
            this.outPath = outPath;
            this.sourceRecommenders = sourceRecommenders;
            this.mainRecommender = mainRecommender;
        }
    }

    private static class DailyResult {
        final Map<Integer, KResult> kResults;
        final String topKRecommendedStr;

        DailyResult(Map<Integer, KResult> kResults, String topKRecommendedStr) {
            this.kResults = kResults;
            this.topKRecommendedStr = topKRecommendedStr;
        }

        static class KResult {
            final double ret;
            final double rank;
            final double portVal;

            KResult(double ret, double rank, double portVal) {
                this.ret = ret;
                this.rank = rank;
                this.portVal = portVal;
            }
        }
    }

    private static class PrefetchedData {
        final Map<Integer, Map<Integer, Float>> transactions = new HashMap<>();
        final Map<Integer, Map<Integer, Float>> allRatios = new HashMap<>();
        final Map<Integer, Map<Integer, float[]>> priceData = new HashMap<>();
        final List<Integer> sortedDates = new ArrayList<>();

        void addDate(int dId) {
            if (sortedDates.isEmpty() || sortedDates.get(sortedDates.size() - 1) != dId) {
                sortedDates.add(dId);
            }
        }

        int getNextDate(int current) {
            int idx = Collections.binarySearch(sortedDates, current);
            if (idx >= 0 && idx + 1 < sortedDates.size()) {
                return sortedDates.get(idx + 1);
            }
            return -1;
        }
    }
}
