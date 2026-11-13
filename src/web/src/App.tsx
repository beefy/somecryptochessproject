/**
 * The application shell: title bar, navigation and the route switch.
 *
 * There is no router dependency: `router.ts` maps `#/blocks/12` to a route and
 * the shell renders the matching view. Keeping the shell thin means each view
 * owns its own request, its loading state and its empty state.
 */

import { lazy, Suspense, useMemo, useState, type ReactNode } from "react";
import { ScrollView, StyleSheet, useWindowDimensions, View } from "react-native";
import { Banner, Button, IconButton, PaperProvider, Surface, Text, useTheme } from "react-native-paper";

import { Anchor, Loading, Page } from "./components/Chrome";
import { formatInteger, shortHash } from "./lib/format";
import { NodeProvider, useNode } from "./node";
import { activeSection, navigate, useRoute, type Route } from "./router";
import { darkTheme, lightTheme, ThemeModeContext, preferredMode, useThemeMode, type ThemeMode } from "./theme";
import { AccountView } from "./views/AccountView";
import { BlockDetail } from "./views/BlockDetail";
import { Blocks } from "./views/Blocks";
import { Dashboard } from "./views/Dashboard";
import { GameDetail } from "./views/GameDetail";
import { Games } from "./views/Games";
import { NotFound } from "./views/NotFound";

/**
 * The table pulls in `chess.js`, which the explorer itself never needs, so it
 * loads on demand: an explorer visit should not pay for a move generator.
 */
const Play = lazy(async () => {
  const module = await import("./views/Play");
  return { default: module.Play };
});

const SECTIONS = [
  { key: "dashboard", label: "Overview", href: "#/" },
  { key: "blocks", label: "Blocks", href: "#/blocks" },
  { key: "games", label: "Games", href: "#/games" },
  { key: "play", label: "Play", href: "#/play" },
] as const;

export function App(): ReactNode {
  const [mode, setMode] = useState<ThemeMode>(preferredMode);
  const theme = useMemo(
    () => ({ mode, toggle: () => setMode(mode === "dark" ? "light" : "dark") }),
    [mode],
  );
  return (
    <ThemeModeContext.Provider value={theme}>
      <PaperProvider theme={mode === "dark" ? darkTheme : lightTheme}>
        <NodeProvider>
          <Shell />
        </NodeProvider>
      </PaperProvider>
    </ThemeModeContext.Provider>
  );
}

function Shell(): ReactNode {
  const route = useRoute();
  const { status } = useNode();
  const { mode, toggle } = useThemeMode();
  const paper = useTheme();
  const { width } = useWindowDimensions();
  const compact = width < 720;
  const section = activeSection(route);
  const offline = status.error !== undefined;

  return (
    <View style={[styles.root, { backgroundColor: paper.colors.background }]}>
      <Surface elevation={2} style={styles.header}>
        <View style={styles.headerRow}>
          <Anchor href="#/" onPress={() => navigate("#/")} style={styles.brand}>
            <Text variant="titleLarge" style={styles.brandText}>
              SCCP
            </Text>
            {!compact && (
              <Text variant="bodySmall" style={styles.brandSub}>
                explorer
              </Text>
            )}
          </Anchor>

          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.nav}>
            {SECTIONS.map((item) => {
              const active = item.key === section;
              return (
                <Anchor key={item.key} href={item.href} onPress={() => navigate(item.href)} style={styles.navItem}>
                  <Text variant="labelLarge" style={active ? styles.navActive : styles.navLabel}>
                    {item.label}
                  </Text>
                  {active && <View style={styles.navUnderline} />}
                </Anchor>
              );
            })}
          </ScrollView>

          <View style={styles.headerRight}>
            {status.data !== undefined && (
              <Text variant="labelLarge" style={styles.height}>
                {formatInteger(status.data.height)}
              </Text>
            )}
            <IconButton
              icon={mode === "dark" ? "weather-sunny" : "weather-night"}
              size={20}
              onPress={toggle}
              accessibilityLabel="Toggle colour scheme"
            />
          </View>
        </View>
        {status.data !== undefined && (
          <View style={styles.statusLine}>
            <Text variant="bodySmall" style={styles.muted}>
              {status.data.network} · tip {shortHash(status.data.tip_hash)} · {status.data.peers}{" "}
              {status.data.peers === 1 ? "peer" : "peers"} · mempool {status.data.mempool.transactions} tx,{" "}
              {status.data.mempool.proofs} {status.data.mempool.proofs === 1 ? "proof" : "proofs"}
            </Text>
          </View>
        )}
      </Surface>

      {offline && (
        <View style={styles.banner}>
          <Banner
            visible
            icon="alert-circle-outline"
            actions={[{ label: "Retry", onPress: () => status.reload() }]}
          >
            {status.error?.message ?? "the node is unreachable"}
          </Banner>
        </View>
      )}

      <Page>
        <RouteView route={route} />
      </Page>
    </View>
  );
}

function RouteView({ route }: { route: Route }): ReactNode {
  switch (route.name) {
    case "dashboard":
      return <Dashboard />;
    case "blocks":
      return <Blocks />;
    case "block":
      return <BlockDetail reference={route.reference} />;
    case "games":
      return <Games />;
    case "game":
      return <GameDetail gameHash={route.gameHash} />;
    case "play":
      return (
        <Suspense fallback={<Loading />}>
          <Play />
        </Suspense>
      );
    case "account":
      return <AccountView address={route.address} />;
    case "notFound":
      return <NotFound path={route.path} />;
  }
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  header: { width: "100%", paddingTop: 8, paddingBottom: 4 },
  headerRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 16,
    paddingHorizontal: 16,
    maxWidth: 1080,
    width: "100%",
    alignSelf: "center",
  },
  brand: { flexDirection: "row", alignItems: "baseline", gap: 6 },
  brandText: { fontWeight: "700", letterSpacing: 1 },
  brandSub: { opacity: 0.6 },
  nav: { gap: 4, alignItems: "center", paddingHorizontal: 4 },
  navItem: { paddingVertical: 8, paddingHorizontal: 10, alignItems: "center" },
  navLabel: { opacity: 0.72 },
  navActive: { fontWeight: "700" },
  navUnderline: {
    height: 2,
    alignSelf: "stretch",
    backgroundColor: "#00639b",
    marginTop: 4,
    borderRadius: 2,
  },
  headerRight: { flexDirection: "row", alignItems: "center", marginLeft: "auto" },
  height: { fontVariant: ["tabular-nums"], opacity: 0.8 },
  statusLine: {
    paddingHorizontal: 16,
    paddingBottom: 6,
    maxWidth: 1080,
    width: "100%",
    alignSelf: "center",
  },
  muted: { opacity: 0.66 },
  banner: { width: "100%" },
});

