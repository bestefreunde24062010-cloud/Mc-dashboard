package de.dashboard;

import com.google.inject.Inject;
import com.velocitypowered.api.command.CommandManager;
import com.velocitypowered.api.command.CommandMeta;
import com.velocitypowered.api.event.Subscribe;
import com.velocitypowered.api.event.proxy.ProxyInitializeEvent;
import com.velocitypowered.api.plugin.Plugin;
import com.velocitypowered.api.plugin.annotation.DataDirectory;
import com.velocitypowered.api.proxy.ProxyServer;
import org.slf4j.Logger;

import java.nio.file.Path;

@Plugin(
    id = "mc-dashboard",
    name = "MC Dashboard",
    version = "1.0.0",
    description = "Verbindet Velocity mit dem Minecraft Web-Dashboard",
    authors = {"DeinName"}
)
public class Dashboard {

    private final ProxyServer proxy;
    private final Logger logger;
    private final Path dataDirectory;

    @Inject
    public Dashboard(
        ProxyServer proxy,
        Logger logger,
        @DataDirectory Path dataDirectory
    ) {
        this.proxy = proxy;
        this.logger = logger;
        this.dataDirectory = dataDirectory;
    }

    @Subscribe
    public void onProxyInitialize(ProxyInitializeEvent event) {
        PluginConfig config = PluginConfig.load(dataDirectory, logger);

        DashboardClient client = new DashboardClient(
            config.dashboardUrl,
            config.apiKey,
            config.httpTimeoutSeconds,
            logger
        );

        CommandManager cm = proxy.getCommandManager();

        CommandMeta meta = cm.metaBuilder("mc-server")
            .aliases("mcs", "dash")
            .plugin(this)
            .build();

        cm.register(meta, new ServerCommand(client, proxy, logger, config));
        logger.info("MC Dashboard aktiviert. Befehl: /mc-server");
    }
}
