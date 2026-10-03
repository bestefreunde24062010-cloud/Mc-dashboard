package de.dashboard;

import com.velocitypowered.api.command.CommandSource;
import com.velocitypowered.api.command.SimpleCommand;
import com.velocitypowered.api.proxy.ProxyServer;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.slf4j.Logger;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

public class ServerCommand implements SimpleCommand {

    public static final String PERM_USE = "dashboard.use";
    public static final String PERM_START = "dashboard.start";
    public static final String PERM_STOP = "dashboard.stop";
    public static final String PERM_RESTART = "dashboard.restart";
    public static final String PERM_STATUS = "dashboard.status";
    public static final String PERM_COMMAND = "dashboard.command";
    public static final String PERM_ADMIN = "dashboard.admin";

    private volatile List<String> cachedServers = new ArrayList<>();
    private volatile long cacheTimestamp = 0L;
    private static final long CACHE_TTL_MS = 30_000L;

    private final DashboardClient client;
    private final ProxyServer proxy;
    private final Logger logger;
    private final PluginConfig config;

    public ServerCommand(
        DashboardClient client,
        ProxyServer proxy,
        Logger logger,
        PluginConfig config
    ) {
        this.client = client;
        this.proxy = proxy;
        this.logger = logger;
        this.config = config;
    }

    @Override
    public void execute(Invocation invocation) {
        CommandSource src = invocation.source();
        String[] args = invocation.arguments();

        if (!src.hasPermission(PERM_USE)
                && !src.hasPermission(PERM_ADMIN)) {
            src.sendMessage(noPerm());
            return;
        }

        if (args.length < 1) {
            printUsage(src);
            return;
        }

        if (args.length < 2) {
            if ("status".equalsIgnoreCase(config.defaultAction)) {
                if (!src.hasPermission(PERM_STATUS)
                        && !src.hasPermission(PERM_ADMIN)) {
                    src.sendMessage(noPerm());
                    return;
                }
                handleStatus(src, args[0]);
            } else {
                printUsage(src);
            }
            return;
        }

        String name = args[0];
        String action = args[1].toLowerCase(Locale.ROOT);

        switch (action) {
            case "start" -> {
                if (!src.hasPermission(PERM_START)
                        && !src.hasPermission(PERM_ADMIN)) {
                    src.sendMessage(noPerm());
                    return;
                }
                runAsync(src, () -> {
                    client.start(name);
                    src.sendMessage(ok(
                        "'" + name + "' was started."
                    ));
                });
            }

            case "stop" -> {
                if (!src.hasPermission(PERM_STOP)
                        && !src.hasPermission(PERM_ADMIN)) {
                    src.sendMessage(noPerm());
                    return;
                }
                runAsync(src, () -> {
                    client.stop(name);
                    src.sendMessage(ok(
                        "'" + name + "' was stopped."
                    ));
                });
            }

            case "restart" -> {
                if (!src.hasPermission(PERM_RESTART)
                        && !src.hasPermission(PERM_ADMIN)) {
                    src.sendMessage(noPerm());
                    return;
                }
                runAsync(src, () -> {
                    client.restart(name);
                    src.sendMessage(ok(
                        "'" + name + "' was restarted."
                    ));
                });
            }

            case "status" -> {
                if (!src.hasPermission(PERM_STATUS)
                        && !src.hasPermission(PERM_ADMIN)) {
                    src.sendMessage(noPerm());
                    return;
                }
                handleStatus(src, name);
            }

            case "command", "cmd" -> {
                if (!src.hasPermission(PERM_COMMAND)
                        && !src.hasPermission(PERM_ADMIN)) {
                    src.sendMessage(noPerm());
                    return;
                }
                if (args.length < 3) {
                    src.sendMessage(Component.text(
                        "Benutzung: /mc-server <name> command <befehl>",
                        NamedTextColor.YELLOW
                    ));
                    return;
                }
                StringBuilder sb = new StringBuilder();
                for (int i = 2; i < args.length; i++) {
                    if (i > 2) sb.append(' ');
                    sb.append(args[i]);
                }
                String cmd = sb.toString();
                runAsync(src, () -> {
                    client.sendCommand(name, cmd);
                    src.sendMessage(ok(
                        "Befehl an '" + name + "' gesendet."
                    ));
                });
            }

            default -> src.sendMessage(Component.text(
                "Unbekannte Aktion: " + action,
                NamedTextColor.RED
            ));
        }
    }

    private void handleStatus(CommandSource src, String name) {
        runAsync(src, () -> {
            String json = client.status(name);
            for (Component c : StatusFormatter.format(json)) {
                src.sendMessage(c);
            }
        });
    }

    @Override
    public boolean hasPermission(Invocation invocation) {
        return invocation.source().hasPermission(PERM_USE)
            || invocation.source().hasPermission(PERM_ADMIN);
    }

    @Override
    public List<String> suggest(Invocation invocation) {
        CommandSource src = invocation.source();
        String[] args = invocation.arguments();
        List<String> out = new ArrayList<>();

        if (!src.hasPermission(PERM_USE)
                && !src.hasPermission(PERM_ADMIN)) {
            return out;
        }

        boolean admin = src.hasPermission(PERM_ADMIN);

        if (args.length <= 1) {
            out.addAll(getCachedServers());
        } else if (args.length == 2) {
            if (admin || src.hasPermission(PERM_START)) out.add("start");
            if (admin || src.hasPermission(PERM_STOP)) out.add("stop");
            if (admin || src.hasPermission(PERM_RESTART)) out.add("restart");
            if (admin || src.hasPermission(PERM_STATUS)) out.add("status");
            if (admin || src.hasPermission(PERM_COMMAND)) out.add("command");
        } else if (args.length >= 3
                && args[1].equalsIgnoreCase("command")) {
            out.add("<befehl>");
        }

        String prefix = args[args.length - 1].toLowerCase(Locale.ROOT);
        out.removeIf(s -> !s.toLowerCase(Locale.ROOT).startsWith(prefix)
            && !s.startsWith("<"));
        return out;
    }

    private List<String> getCachedServers() {
        long now = System.currentTimeMillis();
        if (now - cacheTimestamp < CACHE_TTL_MS
                && !cachedServers.isEmpty()) {
            return cachedServers;
        }

        try {
            List<String> fresh = client.listServers();
            if (fresh != null && !fresh.isEmpty()) {
                cachedServers = fresh;
                cacheTimestamp = now;
            }
            return cachedServers;
        } catch (Exception e) {
            if (cachedServers.isEmpty()) {
                return List.of("<servername>");
            }
            return cachedServers;
        }
    }

    private void runAsync(CommandSource src, ThrowingRunnable task) {
        proxy.getScheduler().buildTask(
            proxy.getPluginManager()
                .getPlugin("mc-dashboard")
                .orElseThrow(),
            () -> {
                try {
                    task.run();
                } catch (Exception e) {
                    logger.warn("Dashboard-Aufruf fehlgeschlagen: {}",
                        e.getMessage());
                    src.sendMessage(Component.text(
                        "Fehler: " + e.getMessage(),
                        NamedTextColor.RED
                    ));
                }
            }
        ).schedule();
    }

    private static void printUsage(CommandSource src) {
        src.sendMessage(Component.text(
            "Benutzung: /mc-server <name> "
          + "start|stop|restart|status|command <befehl>",
            NamedTextColor.YELLOW
        ));
    }

    private static Component ok(String msg) {
        return Component.text("[Dashboard] ", NamedTextColor.GOLD)
            .append(Component.text(msg, NamedTextColor.GREEN));
    }

    private static Component noPerm() {
        return Component.text("[Dashboard] ", NamedTextColor.GOLD)
            .append(Component.text(
                "Du hast keine Berechtigung für diesen Befehl.",
                NamedTextColor.RED
            ));
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }
        }
