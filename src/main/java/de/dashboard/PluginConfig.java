package de.dashboard;

import com.moandjiezana.toml.Toml;
import org.slf4j.Logger;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;

public class PluginConfig {

    public final String dashboardUrl;
    public final String apiKey;
    public final int httpTimeoutSeconds;
    public final String defaultAction;

    private PluginConfig(
        String dashboardUrl,
        String apiKey,
        int httpTimeoutSeconds,
        String defaultAction
    ) {
        this.dashboardUrl = dashboardUrl;
        this.apiKey = apiKey;
        this.httpTimeoutSeconds = httpTimeoutSeconds;
        this.defaultAction = defaultAction;
    }

    public static PluginConfig load(Path dataDirectory, Logger logger) {
        try {
            if (!Files.exists(dataDirectory)) {
                Files.createDirectories(dataDirectory);
            }

            Path configPath = dataDirectory.resolve("config.toml");

            if (!Files.exists(configPath)) {
                copyDefaultConfig(configPath, logger);
            }

            Toml toml = new Toml().read(configPath.toFile());

            String url = toml.getString("dashboard_url", "http://127.0.0.1:8080");
            if (url.endsWith("/")) {
                url = url.substring(0, url.length() - 1);
            }

            String key = toml.getString("api_key", "");
            Long timeout = toml.getLong("http_timeout_seconds", 10L);
            String defAction = toml.getString("default_action", "help");

            if (key == null || key.isEmpty()) {
                logger.warn("Kein api_key in config.toml gesetzt! "
                    + "Dashboard-Aufrufe sind ungeschützt.");
            }

            logger.info("Config geladen: {}", configPath);
            logger.info("Dashboard-URL: {}", url);

            return new PluginConfig(
                url,
                key == null ? "" : key,
                timeout == null ? 10 : timeout.intValue(),
                defAction == null ? "help" : defAction
            );

        } catch (IOException e) {
            logger.error("Konnte config.toml nicht laden/erstellen "
                + "- verwende Defaults.", e);
            return new PluginConfig("http://127.0.0.1:8080", "", 10, "help");
        }
    }

    private static void copyDefaultConfig(Path configPath, Logger logger)
            throws IOException {
        try (InputStream in = PluginConfig.class
                .getResourceAsStream("/config.toml")) {
            if (in != null) {
                Files.copy(in, configPath);
                logger.warn("config.toml wurde erstellt: {} - "
                    + "bitte api_key und dashboard_url anpassen "
                    + "und Velocity neu starten!", configPath);
            } else {
                String fallback =
                    "dashboard_url = \"http://127.0.0.1:8080\"\n"
                  + "api_key = \"\"\n"
                  + "http_timeout_seconds = 10\n"
                  + "default_action = \"help\"\n";
                Files.writeString(configPath, fallback);
                logger.warn("config.toml (Fallback) wurde erstellt: {}",
                    configPath);
            }
        }
    }
}
