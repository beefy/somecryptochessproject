/**
 * The application shell: title bar, navigation and the route switch.
 *
 * There is no router dependency: `router.ts` maps `#/blocks/12` to a route and
 * the shell renders the matching view. Keeping the shell thin means each view
 * owns its own request, its loading state and its empty state.
 */

import { Component, lazy, Suspense, useMemo, useState, type ReactNode } from "react";
import { ScrollView, StyleSheet, useWindowDimensions, View } from "react-native";
import { Banner, Button, PaperProvider, Surface, Text, useTheme } from "react-native-paper";

import { AppMenu } from "./components/AppMenu";
import { Anchor, Loading, Page } from "./components/Chrome";
import { formatInteger, shortHash } from "./lib/format";
import { currentSite } from "./lib/site";
import { NodeProvider, useNode } from "./node";
import { activeSection, navigate, useRoute, type Route } from "./router";
import { darkTheme, lightTheme, ThemeModeContext, preferredMode, useThemeMode, type ThemeMode } from "./theme";
import { AccountView } from "./views/AccountView";
import { BlockDetail } from "./views/BlockDetail";
import { Blocks } from "./views/Blocks";
import { Dashboard } from "./views/Dashboard";
import { GameDetail } from "./views/GameDetail";
import { Games } from "./views/Games";
import { MinerRating } from "./views/MinerRating";
import { NotFound } from "./views/NotFound";
import { Ratings } from "./views/Ratings";

/**
 * The table pulls in `chess.js`, which the explorer itself never needs, so it
 * loads on demand: an explorer visit should not pay for a move generator.
 */
const Play = lazy(async () => {
  const module = await import("./views/Play");
  return { default: module.Play };
});

/**
 * The wallet pulls in scrypt, ML-DSA-65 key derivation and the BIP-39 wordlist.
 * Loaded on demand for the same reason: most explorer visits never open it, and
 * on the wallet subdomain it is the first and only thing asked for.
 */
const Wallet = lazy(async () => {
  const module = await import("./views/Wallet");
  return { default: module.Wallet };
});

const EXPLORER_SECTIONS = [
  { key: "dashboard", label: "Overview", href: "#/" },
  { key: "blocks", label: "Blocks", href: "#/blocks" },
  { key: "games", label: "Games", href: "#/games" },
  { key: "ratings", label: "Elo", href: "#/ratings" },
  { key: "play", label: "Play", href: "#/play" },
] as const;

const WALLET_SECTIONS = [
  { key: "wallet", label: "Overview", href: "#/wallet" },
  { key: "walletSend", label: "Send", href: "#/wallet/send" },
  { key: "walletReceive", label: "Receive", href: "#/wallet/receive" },
  { key: "walletHistory", label: "History", href: "#/wallet/history" },
  { key: "walletSettings", label: "Settings", href: "#/wallet/settings" },
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
  const site = currentSite();
  // The wallet shows its own tabs wherever it is open -- the wallet subdomain,
  // `explorer/#/wallet`, or a dev server -- so "Send" is a tab and not a detour
  // through the hamburger. The hamburger itself only switches sites.
  const walletOpen =
    site === "wallet" ||
    route.name === "wallet" ||
    route.name === "walletSend" ||
    route.name === "walletReceive" ||
    route.name === "walletHistory" ||
    route.name === "walletSettings";
  const section =
    walletOpen && route.name === "dashboard" ? "wallet" : activeSection(route);
  const offline = status.error !== undefined;
  const sections = walletOpen ? WALLET_SECTIONS : EXPLORER_SECTIONS;

  return (
    <View style={[styles.root, { backgroundColor: paper.colors.background }]}>
      <Surface elevation={2} style={styles.header}>
        <View style={styles.headerRow}>
          <AppMenu />

          <Anchor href="#/" onPress={() => navigate(walletOpen ? "#/wallet" : "#/")} style={styles.brand}>
            <Text variant="titleLarge" style={styles.brandText}>
              SCCP
            </Text>
            {!compact && (
              <Text variant="bodySmall" style={styles.brandSub}>
                {walletOpen ? "wallet" : site}
              </Text>
            )}
          </Anchor>

          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.nav}>
            {sections.map((item) => {
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
            {/* A label, not an icon: no icon library ships with this bundle, so
                `IconButton icon="weather-night"` renders an empty circle. */}
            <Button compact mode="text" onPress={toggle} accessibilityLabel="Toggle colour scheme">
              {mode === "dark" ? "Light" : "Dark"}
            </Button>
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
        <RouteBoundary key={routeKey(route)}>
          <RouteView route={route} />
        </RouteBoundary>
      </Page>
    </View>
  );
}

/**
 * Catch a view that throws while rendering.
 *
 * Without this, one bad render unmounts the whole tree and the page goes blank —
 * on a dark theme, a black screen with no error, no header and no navigation, and
 * nothing in the UI to say what happened. A view that throws is a bug in that
 * view, not a reason to take the explorer down: show the message where the view
 * would have been and keep the shell (and the console's stack trace) intact.
 *
 * Keyed by route so navigating away from a broken view clears the error.
 */
class RouteBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  override state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error };
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (error === null) return this.props.children;
    return (
      <View style={styles.crash}>
        <Text variant="titleMedium">This page failed to render</Text>
        <Text variant="bodyMedium" style={styles.crashMessage}>
          {error.message}
        </Text>
        <Text variant="bodySmall" style={styles.muted}>
          The rest of the explorer still works — pick another section, or retry this page. The
          browser console has the stack trace.
        </Text>
        <Button mode="outlined" onPress={() => this.setState({ error: null })}>
          Retry
        </Button>
      </View>
    );
  }
}

/**
 * A stable identity per route, so a boundary reset does not leak between pages.
 *
 * Every wallet route shares one key: the wallet holds its unlocked keypair in
 * component state, and a fresh key per tab would lock the user out on every tab
 * click. Switching tabs changes the `view` prop, not the component's identity.
 */
function routeKey(route: Route): string {
  switch (route.name) {
    case "block":
      return `block:${route.reference}`;
    case "game":
      return `game:${route.gameHash}`;
    case "miner":
      return `miner:${route.address}`;
    case "account":
      return `account:${route.address}`;
    case "notFound":
      return `notFound:${route.path}`;
    case "dashboard":
      return currentSite() === "wallet" ? "wallet" : "dashboard";
    case "wallet":
    case "walletSend":
    case "walletReceive":
    case "walletHistory":
    case "walletSettings":
      return "wallet";
    default:
      return route.name;
  }
}

/**
 * The wallet, lazily: `#/` on the wallet subdomain is its landing page, so the
 * site's default route and `#/wallet` render the same view.
 */
function WalletRoute({
  view,
}: {
  view: "overview" | "send" | "receive" | "history" | "settings";
}): ReactNode {
  return (
    <Suspense fallback={<Loading />}>
      <Wallet view={view} />
    </Suspense>
  );
}

function RouteView({ route }: { route: Route }): ReactNode {
  switch (route.name) {
    case "dashboard":
      return currentSite() === "wallet" ? <WalletRoute view="overview" /> : <Dashboard />;
    case "blocks":
      return <Blocks />;
    case "block":
      return <BlockDetail reference={route.reference} />;
    case "games":
      return <Games />;
    case "game":
      return <GameDetail gameHash={route.gameHash} />;
    case "ratings":
      return <Ratings />;
    case "miner":
      return <MinerRating address={route.address} />;
    case "play":
      return (
        <Suspense fallback={<Loading />}>
          <Play />
        </Suspense>
      );
    case "account":
      return <AccountView address={route.address} />;
    case "wallet":
      return <WalletRoute view="overview" />;
    case "walletSend":
      return <WalletRoute view="send" />;
    case "walletReceive":
      return <WalletRoute view="receive" />;
    case "walletHistory":
      return <WalletRoute view="history" />;
    case "walletSettings":
      return <WalletRoute view="settings" />;
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
  crash: {
    gap: 12,
    padding: 24,
    maxWidth: 720,
    width: "100%",
    alignSelf: "center",
    alignItems: "flex-start",
  },
  crashMessage: { fontFamily: "monospace" },
});

