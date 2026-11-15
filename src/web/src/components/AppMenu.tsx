/**
 * The hamburger in the top-left corner: the switch between the two sites.
 *
 * The explorer and the wallet are separate subdomains serving the same bundle, so
 * "go to the wallet" is a real navigation, not a hash change -- a bookmark of
 * `wallet.chessproofofwork.com` has to stay that. On a development server there is
 * no second name, so the same menu switches with a hash (`#/wallet`), which keeps
 * the UI testable locally.
 *
 * The glyph is three `View`s rather than `IconButton icon="menu"`: react-native-paper
 * renders icons through an icon library, and this bundle has none installed, so an
 * icon button would be an invisible tap target. Primitives cannot go missing.
 */

import { useState, type ReactNode } from "react";
import { Pressable, StyleSheet, View } from "react-native";
import { Menu, useTheme } from "react-native-paper";

import { SITE_LABELS, crossSiteHref, currentSite, hashUrl, type Site } from "../lib/site";
import { navigate } from "../router";

/** Navigate to `hash` on `site`, crossing the subdomain only when hosted. */
function open(site: Site, hash: string): void {
  if (site === currentSite()) {
    navigate(hash);
    return;
  }
  const base = crossSiteHref(site);
  if (base.startsWith("#")) {
    navigate(hash);
    return;
  }
  // `base` ends in `/`; joining it to `#/wallet` naively left `host//#/wallet`.
  globalThis.location.assign(hashUrl(base, hash));
}

function HamburgerButton({ onPress }: { onPress: () => void }): ReactNode {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel="Open the menu to switch between the explorer and the wallet"
      style={styles.button}
      hitSlop={8}
    >
      {[0, 1, 2].map((bar) => (
        <View key={bar} style={[styles.bar, { backgroundColor: colors.onSurface }]} />
      ))}
    </Pressable>
  );
}

export function AppMenu(): ReactNode {
  const [visible, setVisible] = useState(false);
  const close = (action: () => void) => () => {
    setVisible(false);
    action();
  };

  return (
    <Menu
      visible={visible}
      onDismiss={() => setVisible(false)}
      anchorPosition="bottom"
      anchor={<HamburgerButton onPress={() => setVisible(true)} />}
    >
      {(Object.keys(SITE_LABELS) as Site[]).map((site) => (
        <Menu.Item
          key={site}
          title={SITE_LABELS[site]}
          onPress={close(() => open(site, site === "wallet" ? "#/wallet" : "#/"))}
        />
      ))}
    </Menu>
  );
}

const styles = StyleSheet.create({
  button: {
    width: 40,
    height: 40,
    borderRadius: 20,
    justifyContent: "center",
    alignItems: "center",
    gap: 4,
  },
  bar: { width: 18, height: 2, borderRadius: 1 },
});
