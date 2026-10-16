package stockguidance.util;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

public class JsonUtils {

    public static String extractString(String content, String key) {
        Pattern p = Pattern.compile("\"" + key + "\":\\s*\"([^\"]*)\"");
        Matcher m = p.matcher(content);
        if (m.find()) return m.group(1);
        return null;
    }

    public static Integer extractInt(String content, String key) {
        Pattern p = Pattern.compile("\"" + key + "\":\\s*(\\d+)");
        Matcher m = p.matcher(content);
        if (m.find()) return Integer.parseInt(m.group(1));
        return null;
    }

    public static Double extractDouble(String content, String key) {
        Pattern p = Pattern.compile("\"" + key + "\":\\s*([\\d\\.]+)");
        Matcher m = p.matcher(content);
        if (m.find()) return Double.parseDouble(m.group(1));
        return null;
    }

    public static List<Integer> extractIntList(String content, String key) {
        // More robust pattern that handles optional spaces and newlines around brackets
        Pattern p = Pattern.compile("\"" + key + "\":\\s*\\[([^\\]]*)\\]", Pattern.DOTALL);
        Matcher m = p.matcher(content);
        if (m.find()) {
            String listStr = m.group(1).trim();
            if (listStr.isEmpty() || listStr.equals("null")) return new ArrayList<>();
            try {
                return Arrays.stream(listStr.split(","))
                        .map(String::trim)
                        .filter(s -> !s.isEmpty() && !s.equals("null"))
                        .map(Integer::parseInt)
                        .collect(Collectors.toList());
            } catch (NumberFormatException e) {
                return new ArrayList<>();
            }
        }
        return null;
    }

    public static List<String> extractStringList(String content, String key) {
        Pattern p = Pattern.compile("\"" + key + "\":\\s*\\[([^\\]]*)\\]");
        Matcher m = p.matcher(content);
        if (m.find()) {
            String listStr = m.group(1);
            if (listStr.trim().isEmpty()) return new ArrayList<>();
            Pattern stringPattern = Pattern.compile("\"([^\"]*)\"");
            Matcher stringMatcher = stringPattern.matcher(listStr);
            List<String> result = new ArrayList<>();
            while (stringMatcher.find()) {
                result.add(stringMatcher.group(1));
            }
            return result;
        }
        return null;
    }

    public static Boolean extractBoolean(String content, String key) {
        Pattern p = Pattern.compile("\"" + key + "\":\\s*(true|false)");
        Matcher m = p.matcher(content);
        if (m.find()) return Boolean.parseBoolean(m.group(1));
        return null;
    }
}
