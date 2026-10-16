package stockguidance.models;

import stockguidance.util.JsonUtils;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class SingleRun {
    public String timestampId;
    public String dayChangeCentered;
    public List<Integer> tickerIds;
    public Integer inputSize;
    public Integer outputSize;
    public Integer priorDays;
    public FamilyRun familyRun;

    @Override
    public String toString() {
        return "SingleRun{" +
                "timestampId='" + timestampId + '\'' +
                ", dayChangeCentered='" + dayChangeCentered + '\'' +
                ", priorDays=" + priorDays +
                ", inputSize=" + inputSize +
                ", outputSize=" + outputSize +
                ", tickerIdsCount=" + (tickerIds != null ? tickerIds.size() : 0) +
                '}';
    }

    public static SingleRun load(File file) throws IOException {
        String content = new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8);
        SingleRun sr = new SingleRun();

        String singleRunBlock = content;
        Pattern blockPattern = Pattern.compile("\"single_run\"\\s*:\\s*\\{(.*)", Pattern.DOTALL);
        Matcher blockMatcher = blockPattern.matcher(content);
        if (blockMatcher.find()) {
            singleRunBlock = blockMatcher.group(1);
        }

        sr.timestampId = JsonUtils.extractString(singleRunBlock, "timestamp_id");
        sr.dayChangeCentered = JsonUtils.extractString(singleRunBlock, "day_change_centered");
        sr.priorDays = JsonUtils.extractInt(singleRunBlock, "prior_days");
        sr.inputSize = JsonUtils.extractInt(singleRunBlock, "input_size");
        sr.outputSize = JsonUtils.extractInt(singleRunBlock, "output_size");

        sr.tickerIds = JsonUtils.extractIntList(singleRunBlock, "ticker_ids");
        if (sr.tickerIds == null || sr.tickerIds.isEmpty()) {
            // Try to extract from the root or elsewhere, but be careful of the "single_runs_for_all_combinations" list
            // Just look for the first occurrence of "ticker_ids" that isn't null in the whole file if the block failed
            sr.tickerIds = JsonUtils.extractIntList(content, "ticker_ids");
        }

        if (sr.tickerIds == null || sr.tickerIds.isEmpty()) {
            // Second attempt: if it's "ticker_ids": null, it might be in the family_run section
            Pattern p = Pattern.compile("\"ticker_ids\"\\s*:\\s*\\[([^\\]]*)\\]", Pattern.DOTALL);
            Matcher m = p.matcher(content);
            while (m.find()) {
                String idsContent = m.group(1).trim();
                if (idsContent.isEmpty() || idsContent.equals("null")) continue;
                List<Integer> ids = JsonUtils.extractIntList("\"ticker_ids\": [" + idsContent + "]", "ticker_ids");
                if (ids != null && !ids.isEmpty()) {
                    sr.tickerIds = ids;
                    break;
                }
            }
        }

        if (sr.tickerIds == null || sr.tickerIds.isEmpty()) {
            // Fourth attempt: searching for family_state.json in parent...
            File pDir = file.getParentFile();
            if (pDir != null) {
                File familyState = new File(pDir, "family_state.json");
                if (!familyState.exists()) {
                    familyState = new File(pDir, "family_run.json");
                }
                if (!familyState.exists() && pDir.getParentFile() != null) {
                    familyState = new File(pDir.getParentFile(), "family_state.json");
                    if (!familyState.exists()) {
                        familyState = new File(pDir.getParentFile(), "family_run.json");
                    }
                }
                if (familyState.exists()) {
                    try {
                        String familyContent = new String(Files.readAllBytes(familyState.toPath()), StandardCharsets.UTF_8);
                        // Search for the first non-null ticker_ids list in family state
                        Pattern pIds = Pattern.compile("\"ticker_ids\"\\s*:\\s*\\[([^\\]]+)\\]", Pattern.DOTALL);
                        Matcher mIds = pIds.matcher(familyContent);
                        while (mIds.find()) {
                            String idsContent = mIds.group(1).trim();
                            if (idsContent.isEmpty() || idsContent.equals("null")) continue;
                            List<Integer> ids = new ArrayList<>();
                            for (String s : idsContent.split(",")) {
                                String trimmed = s.trim();
                                if (!trimmed.isEmpty() && !trimmed.equals("null")) {
                                    try {
                                        ids.add(Integer.parseInt(trimmed));
                                    } catch (NumberFormatException e) {}
                                }
                            }
                            if (!ids.isEmpty()) {
                                sr.tickerIds = ids;
                                break;
                            }
                        }
                    } catch (IOException e) {
                        // ignore
                    }
                }
            }
        }

        sr.familyRun = FamilyRun.loadFromBlock(content);

        return sr;
    }
}
