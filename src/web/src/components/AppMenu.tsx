/**
 * The hamburger in the top-left corner: the switch between the five sites.
 *
 * Explorer, Wallet, Play, DEX and Bridge are separate names serving the same
 * bundle, but the menu switches with a hash instead of a cross-origin navigation.
 * The unlocked keypair lives only in this tab's memory, and leaving the origin
 * would silently lock it. Each public name remains a direct entry point; inside
 * the app, the menu is one session.
 *
 * The glyph is three `View`s rather than `IconButton icon="menu"`: react-native-paper
 * renders icons through an icon library, and this bundle has none installed, so an
 * icon button would be an invisible tap target. Primitives cannot go missing.
 */

import { useState, type ReactNode } from "react";
import { Pressable, StyleSheet, View } from "react-native";
import { Menu, useTheme } from "react-native-paper";

import { SITE_LABELS, siteHash, type Site } from "../lib/site";
import { navigate } from "../router";

function HamburgerButton({ onPress }: { onPress: () => void }): ReactNode {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel="Open the menu to switch between Explorer, Wallet, Play, DEX and Bridge"
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
  const close =
    (site: Site) =>
    (): void => {
      setVisible(false);
      navigate(siteHash(site));
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
          onPress={close(site)}
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
