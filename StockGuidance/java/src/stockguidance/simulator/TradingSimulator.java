package stockguidance.simulator;

import stockguidance.models.FamilyRun;
import stockguidance.models.SingleRun;

import java.io.File;
import java.io.IOException;
import java.util.*;
import java.util.regex.Pattern;

public class TradingSimulator {

    private final List<Integer> topK;
    private final String dbinfoFile;
    private final boolean onlyEnsemble;
    private final boolean force;
    private static final String JAVA_TAG = "_java";

    public TradingSimulator(List<Integer> topK, String dbinfoFile, boolean onlyEnsemble, boolean force) {
        this.topK = topK != null ? topK : Arrays.asList(1, 3, 5, 10);
        this.dbinfoFile = dbinfoFile != null ? dbinfoFile : "dbinfo.id";
        this.onlyEnsemble = onlyEnsemble;
        this.force = force;
    }

    public List<String> generateSimulationsFromDirectories(List<String> directories) {
        List<String> allGenerated = new ArrayList<>();
        for (String d : directories) {
            File path = new File(d);
            if (path.isFile() && path.getName().endsWith(".json")) {
                System.out.println("Processing JSON file: " + d);
                if (path.getName().equals("family_state.json") || path.getName().equals("family_run.json")) {
                    try {
                        FamilyRun familyRun = FamilyRun.load(path);
                        System.out.println("Loaded FamilyRun from direct JSON: " + familyRun);
                    } catch (IOException e) {
                        System.out.println("Error loading FamilyRun from " + d + ": " + e.getMessage());
                    }
                    allGenerated.addAll(generateSimulationsFromDirectories(Arrays.asList(path.getParent())));
                } else {
                    allGenerated.addAll(runSimulationForSingleOrEnsemble(path.getParentFile()));
                }
                continue;
            }
            if (!path.isDirectory()) {
                System.out.println("Warning: " + d + " is not a directory or a valid JSON file. Skipping.");
                continue;
            }
            File dir = path;

            if (isFamilyDirectory(dir)) {
                System.out.println("Processing family directory: " + d);

                // Try to load family run config
                File familyState = new File(dir, "family_state.json");
                if (!familyState.exists()) {
                    familyState = new File(dir, "family_run.json");
                }
                if (familyState.exists()) {
                    try {
                        FamilyRun familyRun = FamilyRun.load(familyState);
                        System.out.println("Loaded FamilyRun: " + familyRun);
                    } catch (IOException e) {
                        System.out.println("Error loading FamilyRun from " + familyState.getAbsolutePath() + ": " + e.getMessage());
                    }
                }

                List<String> subRuns = new ArrayList<>();
                findSubRunsForGivenRunDirectory(dir, subRuns);
                List<String> tempGenerated = generateSimulationsFromDirectories(subRuns);
                allGenerated.addAll(tempGenerated);
            } else if (isEnsembleDirectoryGiven(dir)) {
                System.out.println("Processing ensemble directory: " + d);
                allGenerated.addAll(handleSingleRunDirectory(dir));
            } else if (isRepDirectory(dir)) {
                System.out.println("Processing rep directory: " + d);
                allGenerated.addAll(runSimulationForSingleOrEnsemble(dir));
            } else {
                if (dirHasModel(dir) || findSingleRunPath(dir) != null) {
                    System.out.println("Processing single run directory: " + d);
                    allGenerated.addAll(runSimulationForSingleOrEnsemble(dir));
                } else {
                    System.out.println("INVALID DIRECTORY: " + d);
                }
            }
        }
        return allGenerated;
    }

    private boolean isFamilyDirectory(File path) {
        File familyState = new File(path, "family_state.json");
        return familyState.exists() && familyState.isFile();
    }

    private boolean isEnsembleDirectoryGiven(File path) {
        if (isFamilyDirectory(path)) return false;
        File[] files = path.listFiles();
        if (files == null) return false;
        long repCount = Arrays.stream(files)
                .filter(f -> f.isDirectory() && f.getName().toLowerCase().startsWith("rep"))
                .count();
        return repCount > 1;
    }

    private boolean isRepDirectory(File path) {
        if (!path.isDirectory()) return false;
        File[] files = path.listFiles();
        if (files == null) return false;
        for (File f : files) {
            if (f.isFile() && f.getName().startsWith("stock_scores_epoch_") && f.getName().endsWith(".csv") && f.getName().contains("_net_")) {
                return true;
            }
        }
        return false;
    }

    private boolean dirHasModel(File dir) {
        return new File(dir, "manifest.json").exists();
    }

    private String findSingleRunPath(File dir) {
        File[] files = dir.listFiles();
        if (files == null) return null;
        for (File f : files) {
            if (f.isFile() && (f.getName().equals("single_run_epoch.json") || (f.getName().startsWith("single_run_") && f.getName().endsWith(".json")))) {
                return f.getAbsolutePath();
            }
        }
        if (dir.getName().toLowerCase().contains("rep")) {
            File parent = dir.getParentFile();
            if (parent != null) {
                File[] pFiles = parent.listFiles();
                if (pFiles != null) {
                    for (File f : pFiles) {
                        if (f.isFile() && (f.getName().equals("single_run_epoch.json") || (f.getName().startsWith("single_run_") && f.getName().endsWith(".json")))) {
                            return f.getAbsolutePath();
                        }
                    }
                }
            }
        }
        return null;
    }

    private void findSubRunsForGivenRunDirectory(File runDir, List<String> subRuns) {
        File[] entries = runDir.listFiles();
        if (entries == null) return;
        Arrays.sort(entries);
        for (File entry : entries) {
            if (!entry.isDirectory()) continue;

            String name = entry.getName();
            boolean isSubRun = false;
            if (Pattern.matches("run_single_\\d{8}_\\d{6}_\\d{2,}", name) ||
                Pattern.matches("run_single_\\d{8}_\\d{6}_\\d", name) ||
                Pattern.matches("\\d{8}_\\d{6}", name)) {
                isSubRun = true;
            } else {
                File[] subEntries = entry.listFiles();
                if (subEntries != null) {
                    for (File subF : subEntries) {
                        if (subF.isFile() && (subF.getName().equals("single_run_epoch.json") || (subF.getName().startsWith("single_run_") && subF.getName().endsWith(".json")))) {
                            isSubRun = true;
                            break;
                        }
                    }
                }
            }
            if (isSubRun) {
                subRuns.add(entry.getAbsolutePath());
            }
        }
    }

    private List<String> handleSingleRunDirectory(File runDir) {
        return runSimulationForSingleOrEnsemble(runDir);
    }

    private List<String> runSimulationForSingleOrEnsemble(File runDir) {
        System.out.println("Simulating for: " + runDir.getAbsolutePath());
        List<String> generatedPaths = new ArrayList<>();

        List<File> scoreDirs = getScoreDirs(runDir);
        List<String> epochs = identifyEpochsWithScores(scoreDirs);

        if (epochs.isEmpty()) {
            System.out.println("No score files found in " + runDir.getAbsolutePath());
            return generatedPaths;
        }

        String singleRunPath = findSingleRunPath(runDir);
        if (singleRunPath == null) {
            System.out.println("Error: Could not find single_run_epoch.json for " + runDir.getAbsolutePath());
            return generatedPaths;
        }

        try {
            SingleRun singleRun = SingleRun.load(new File(singleRunPath));
            System.out.println("Loaded SingleRun: " + singleRun);

            List<StockRecommender> recommenders = new ArrayList<>();
            // Recommender for the main directory
            recommenders.add(new StockRecommenderFromSavedScores(singleRun, runDir.getAbsolutePath()));

            // Recommenders for each rep
            for (File scoreDir : scoreDirs) {
                if (scoreDir.equals(runDir)) continue;
                recommenders.add(new StockRecommenderFromSavedScores(singleRun, scoreDir.getAbsolutePath()));
            }

            SimulationEngine engine = new SimulationEngine(recommenders, topK, dbinfoFile, onlyEnsemble, force);

            for (String epoch : epochs) {
                List<Integer> dates = identifyDatesForEpoch(scoreDirs, epoch);
                try {
                    List<String> paths = engine.runSimulation(epoch, dates);
                    generatedPaths.addAll(paths);
                } catch (Exception e) {
                    System.out.println("Error running simulation for epoch " + epoch + ": " + e.getMessage());
                    e.printStackTrace();
                }
            }
        } catch (IOException e) {
            System.out.println("Error loading SingleRun from " + singleRunPath + ": " + e.getMessage());
            return generatedPaths;
        }

        return generatedPaths;
    }

    private List<Integer> identifyDatesForEpoch(List<File> scoreDirs, String epoch) {
        Set<Integer> dates = new HashSet<>();
        Pattern pattern = Pattern.compile("stock_scores_epoch_" + epoch + "_date_(\\d+)_net_.*\\.csv");
        for (File dir : scoreDirs) {
            File[] files = dir.listFiles();
            if (files == null) continue;
            for (File f : files) {
                java.util.regex.Matcher matcher = pattern.matcher(f.getName());
                if (matcher.find()) {
                    dates.add(Integer.parseInt(matcher.group(1)));
                }
            }
        }
        List<Integer> sortedDates = new ArrayList<>(dates);
        Collections.sort(sortedDates);
        return sortedDates;
    }

    private List<File> getScoreDirs(File runDir) {
        List<File> scoreDirs = new ArrayList<>();
        if (isRepDirectory(runDir)) {
            scoreDirs.add(runDir);
        } else {
            File[] files = runDir.listFiles();
            if (files != null) {
                for (File f : files) {
                    if (f.isDirectory() && f.getName().toLowerCase().startsWith("rep") && isRepDirectory(f)) {
                        scoreDirs.add(f);
                    }
                }
            }
            if (scoreDirs.isEmpty()) {
                scoreDirs.add(runDir);
            }
        }
        return scoreDirs;
    }

    private List<String> identifyEpochsWithScores(List<File> scoreDirs) {
        Set<String> epochs = new HashSet<>();
        Pattern pattern = Pattern.compile("stock_scores_epoch_(\\d+)_date_.*\\.csv");
        for (File dir : scoreDirs) {
            File[] files = dir.listFiles();
            if (files == null) continue;
            for (File f : files) {
                java.util.regex.Matcher matcher = pattern.matcher(f.getName());
                if (matcher.find()) {
                    epochs.add(matcher.group(1));
                }
            }
        }
        List<String> sortedEpochs = new ArrayList<>(epochs);
        Collections.sort(sortedEpochs, (a, b) -> Integer.compare(Integer.parseInt(a), Integer.parseInt(b)));
        return sortedEpochs;
    }

    public static void main(String[] args) {
        if (args.length == 0) {
            System.out.println("Usage: TradingSimulator [--force] [--only-ensemble] <directory1> <directory2> ...");
            return;
        }
        boolean forceFlag = false;
        boolean onlyEnsembleFlag = false;
        List<String> dirs = new ArrayList<>();
        for (String arg : args) {
            if (arg.equals("--force")) forceFlag = true;
            else if (arg.equals("--only-ensemble")) onlyEnsembleFlag = true;
            else dirs.add(arg);
        }
        TradingSimulator simulator = new TradingSimulator(Arrays.asList(1, 3, 5, 10), "dbinfo.id", onlyEnsembleFlag, forceFlag);
        List<String> result = simulator.generateSimulationsFromDirectories(dirs);
        System.out.println("Generated simulations: " + result);
    }
}
