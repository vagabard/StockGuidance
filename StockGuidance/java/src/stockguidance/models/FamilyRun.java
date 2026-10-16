package stockguidance.models;

import stockguidance.util.JsonUtils;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class FamilyRun {
    public String initialRunId;
    public List<String> dayChangeCenteredList;
    public List<Integer> priorDaysList;
    public List<Integer> mlpLayersList;
    public List<Integer> epochsList;
    public Boolean completed;
    public Boolean currentDayChange;
    public Boolean volumeRatio;
    public Boolean vwapRatio;
    public Boolean transactionRatio;

    @Override
    public String toString() {
        return "FamilyRun{" +
                "initialRunId='" + initialRunId + '\'' +
                ", dayChangeCenteredList=" + dayChangeCenteredList +
                ", priorDaysList=" + priorDaysList +
                ", mlpLayersList=" + mlpLayersList +
                ", epochsList=" + epochsList +
                ", completed=" + completed +
                '}';
    }

    public static FamilyRun load(File file) throws IOException {
        String content = new String(Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8);
        return loadFromBlock(content);
    }

    public static FamilyRun loadFromBlock(String content) {
        FamilyRun fr = new FamilyRun();

        String familyRunBlock = content;
        Pattern blockPattern = Pattern.compile("\"family_run\"\\s*:\\s*\\{(.*)", Pattern.DOTALL);
        Matcher blockMatcher = blockPattern.matcher(content);
        if (blockMatcher.find()) {
            familyRunBlock = blockMatcher.group(1);
        }

        fr.initialRunId = JsonUtils.extractString(familyRunBlock, "initial_run_id");
        fr.dayChangeCenteredList = JsonUtils.extractStringList(familyRunBlock, "day_change_centered_list");
        fr.priorDaysList = JsonUtils.extractIntList(familyRunBlock, "prior_days_list");
        fr.mlpLayersList = JsonUtils.extractIntList(familyRunBlock, "mlp_layers_list");
        fr.epochsList = JsonUtils.extractIntList(familyRunBlock, "epochs_list");
        fr.completed = JsonUtils.extractBoolean(familyRunBlock, "completed");
        fr.currentDayChange = JsonUtils.extractBoolean(familyRunBlock, "current_day_change");
        fr.volumeRatio = JsonUtils.extractBoolean(familyRunBlock, "volume_ratio");
        fr.vwapRatio = JsonUtils.extractBoolean(familyRunBlock, "vwap_ratio");
        fr.transactionRatio = JsonUtils.extractBoolean(familyRunBlock, "transaction_ratio");

        return fr;
    }
}
