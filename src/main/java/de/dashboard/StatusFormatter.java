package de.dashboard;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.event.HoverEvent;
import net.kyori.adventure.text.format.NamedTextColor;
import net.kyori.adventure.text.format.TextDecoration;

import java.util.ArrayList;
import java.util.List;

public final class StatusFormatter {

    private StatusFormatter() {}

    public static List<Component> format(String json) {
        List<Component> lines = new ArrayList<>();

        JsonObject obj;
        try {
            obj = JsonParser.parseString(json).getAsJsonObject();
        } catch (Exception e) {
            lines.add(Component.text(
                "Ungültige Antwort vom Dashboard.",
                NamedTextColor.RED
            ));
            lines.add(Component.text(json, NamedTextColor.DARK_GRAY));
            return lines;
        }

        if (obj.has("ok") && !obj.get("ok").getAsBoolean()) {
            String err = obj.has("error")
                ? obj.get("error").getAsString()
                : "Unbekannter Fehler";
            lines.add(Component.text("✖ ", NamedTextColor.RED)
                .append(Component.text(err, NamedTextColor.RED)));
            return lines;
        }

        String name = getStr(obj, "name", "?");
        boolean running = obj.has("running")
            && obj.get("running").getAsBoolean();
        int port = getInt(obj, "port", 0);
        int id = getInt(obj, "id", 0);
        int ramAssigned = getInt(obj, "ram_assigned_gb", 0);
        String ramUsed = obj.has("ram_used_gb")
            && !obj.get("ram_used_gb").isJsonNull()
            ? obj.get("ram_used_gb").getAsString()
            : null;

        lines.add(Component.text("┌─────────────────────────────────────┐")
            .color(NamedTextColor.DARK_GRAY));

        lines.add(Component.text("│ ")
            .color(NamedTextColor.DARK_GRAY)
            .append(Component.text("Server: ", NamedTextColor.GRAY))
            .append(Component.text(name, NamedTextColor.GOLD,
                TextDecoration.BOLD))
            .append(Component.text("  (ID " + id + ")",
                NamedTextColor.DARK_GRAY)));

        Component statusComp = running
            ? Component.text("● LÄUFT", NamedTextColor.GREEN,
                TextDecoration.BOLD)
            : Component.text("● GESTOPPT", NamedTextColor.RED,
                TextDecoration.BOLD);

        lines.add(Component.text("│ ")
            .color(NamedTextColor.DARK_GRAY)
            .append(Component.text("Status: ", NamedTextColor.GRAY))
            .append(statusComp));

        lines.add(Component.text("│ ")
            .color(NamedTextColor.DARK_GRAY)
            .append(Component.text("Port:   ", NamedTextColor.GRAY))
            .append(Component.text(String.valueOf(port),
                NamedTextColor.AQUA)));

        Component ramComp;
        if (running && ramUsed != null) {
            ramComp = Component.text(
                ramUsed + " GB / " + ramAssigned + " GB",
                NamedTextColor.AQUA
            );
        } else if (running) {
            ramComp = Component.text(
                "nicht verfügbar / " + ramAssigned + " GB (zugewiesen)",
                NamedTextColor.YELLOW
            );
        } else {
            ramComp = Component.text(
                "0 GB / " + ramAssigned + " GB",
                NamedTextColor.DARK_GRAY
            );
        }

        lines.add(Component.text("│ ")
            .color(NamedTextColor.DARK_GRAY)
            .append(Component.text("RAM:    ", NamedTextColor.GRAY))
            .append(ramComp));

        if (running && ramUsed != null) {
            double used = parseDouble(ramUsed, 0);
            double pct = ramAssigned > 0
                ? Math.min(100, used / ramAssigned * 100) : 0;
            lines.add(Component.text("│ ")
                .color(NamedTextColor.DARK_GRAY)
                .append(Component.text("RAM-Auslastung: ",
                    NamedTextColor.GRAY))
                .append(buildBar(pct))
                .append(Component.text(
                    String.format(" %.1f%%", pct),
                    NamedTextColor.WHITE
                )));
        }

        lines.add(Component.text("└─────────────────────────────────────┘")
            .color(NamedTextColor.DARK_GRAY));

        return lines;
    }

    private static Component buildBar(double pct) {
        int total = 20;
        int filled = (int) Math.round(pct / 100.0 * total);
        if (filled < 0) filled = 0;
        if (filled > total) filled = total;

        NamedTextColor color;
        if (pct < 60) color = NamedTextColor.GREEN;
        else if (pct < 85) color = NamedTextColor.YELLOW;
        else color = NamedTextColor.RED;

        Component bar = Component.text("[", NamedTextColor.DARK_GRAY);
        for (int i = 0; i < total; i++) {
            if (i < filled) bar = bar.append(Component.text("█", color));
            else bar = bar.append(Component.text("░", NamedTextColor.DARK_GRAY));
        }
        bar = bar.append(Component.text("]", NamedTextColor.DARK_GRAY));
        bar = bar.hoverEvent(HoverEvent.showText(
            Component.text(String.format("Auslastung: %.1f%%", pct), color)
        ));
        return bar;
    }

    private static String getStr(JsonObject o, String key, String def) {
        return o.has(key) && !o.get(key).isJsonNull()
            ? o.get(key).getAsString() : def;
    }

    private static int getInt(JsonObject o, String key, int def) {
        try {
            return o.has(key) && !o.get(key).isJsonNull()
                ? o.get(key).getAsInt() : def;
        } catch (Exception e) {
            return def;
        }
    }

    private static double parseDouble(String s, double def) {
        try { return Double.parseDouble(s); }
        catch (Exception e) { return def; }
    }
}
