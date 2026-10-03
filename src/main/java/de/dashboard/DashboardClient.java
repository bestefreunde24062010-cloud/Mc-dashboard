package de.dashboard;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.slf4j.Logger;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;

public class DashboardClient {

    private final String baseUrl;
    private final String apiKey;
    private final int timeoutSeconds;
    private final Logger logger;
    private final HttpClient http;

    public DashboardClient(
        String baseUrl,
        String apiKey,
        int timeoutSeconds,
        Logger logger
    ) {
        this.baseUrl = baseUrl.endsWith("/")
            ? baseUrl.substring(0, baseUrl.length() - 1)
            : baseUrl;
        this.apiKey = apiKey == null ? "" : apiKey;
        this.timeoutSeconds = timeoutSeconds;
        this.logger = logger;

        this.http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(timeoutSeconds))
            .build();
    }

    public String status(String serverName) throws Exception {
        return get("/velocity/" + enc(serverName) + "/status");
    }

    public String start(String serverName) throws Exception {
        return get("/velocity/" + enc(serverName) + "/start");
    }

    public String stop(String serverName) throws Exception {
        return get("/velocity/" + enc(serverName) + "/stop");
    }

    public String restart(String serverName) throws Exception {
        return get("/velocity/" + enc(serverName) + "/restart");
    }

    public String sendCommand(String serverName, String cmd)
            throws Exception {
        return get("/velocity/" + enc(serverName)
                 + "/command?cmd=" + enc(cmd));
    }

    public List<String> listServers() throws Exception {
        String json = get("/velocity/servers");

        JsonObject obj = JsonParser.parseString(json).getAsJsonObject();
        List<String> out = new ArrayList<>();

        if (obj.has("servers")) {
            for (JsonElement e : obj.getAsJsonArray("servers")) {
                out.add(e.getAsString());
            }
        }
        return out;
    }

    private String get(String path) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder()
            .uri(URI.create(baseUrl + path))
            .timeout(Duration.ofSeconds(timeoutSeconds))
            .GET();

        if (!apiKey.isEmpty()) {
            b.header("X-API-Key", apiKey);
        }

        HttpResponse<String> res = http.send(
            b.build(),
            HttpResponse.BodyHandlers.ofString()
        );

        if (res.statusCode() >= 400) {
            throw new RuntimeException(
                "HTTP " + res.statusCode() + ": " + res.body()
            );
        }

        return res.body();
    }

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }
}
