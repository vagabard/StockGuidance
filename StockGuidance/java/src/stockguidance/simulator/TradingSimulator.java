package stockguidance.simulator;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.*;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.stream.Stream;

public class TradingSimulator {

    private final List<Integer> topK;
    private final String dbinfoFile;
    private final boolean onlyEnsemble;
    private final boolean force;

    public TradingSimulator(List<Integer> topK, String dbinfoFile, boolean onlyEnsemble, boolean force) {
        this.topK = topK != null ? topK : Arrays.asList(1, 3, 5, 10);
        this.dbinfoFile = dbinfoFile != null ? dbinfoFile : "dbinfo.id";
        this.onlyEnsemble = onlyEnsemble;
        this.force = force;
    }

    public List<String> generateSimulationsFromDirectories(List<String> directories) {
        List<String> allGenerated = new ArrayList<>();
        for (String d : directories) {
            File dir = new File(d);
            if (!dir.isDirectory()) {
                System.out.println("Warning: " + d + " is not a directory. Skipping.");
                continue;
            }

            if (isFamilyDirectory(dir)) {
                System.out.println("Processing family directory: " + d);
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
        // In Python it uses resolve_manifest_path(run_dir)
        // Simplified check: manifest.json or similar
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
        // Check parent if it's a rep dir
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
        // This would involve loading SingleRun, identifying epochs, and calling performAnalysis.
        // For the purpose of this replication, we'll provide the structural implementation.
        List<String> generatedPaths = new ArrayList<>();

        // 1. Identify score directories
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

        // Logic to process each epoch...
        // This would call performAnalysis(recommenders, topK, ...)

        System.out.println("Replicated logic would now process " + epochs.size() + " epochs.");

        return generatedPaths;
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
        Collections.sort(sortedEpochs, Comparator.comparingInt(Integer::parseInt));
        return sortedEpochs;
    }

    public static void main(String[] args) {
        if (args.length == 0) {
            System.out.println("Usage: TradingSimulator <directory1> <directory2> ...");
            return;
        }
        TradingSimulator simulator = new TradingSimulator(Arrays.asList(1, 3, 5, 10), "dbinfo.id", false, false);
        List<String> result = simulator.generateSimulationsFromDirectories(Arrays.asList(args));
        System.out.println("Generated simulations: " + result);
    }
}
