/**
 * An unknown route.
 *
 * A hash router cannot 404 at the HTTP level, so this is where a stale shared
 * link lands: say what was asked for and offer the two ways out.
 */

import type { ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Button, Text } from "react-native-paper";

import { Mono, Section } from "../components/Chrome";
import { navigate } from "../router";

export function NotFound({ path }: { path: string }): ReactNode {
  return (
    <Section title="Nothing at this address" subtitle="the link may be old, or the route may have changed">
      <View style={styles.body}>
        <Mono>{`#/${path}`}</Mono>
        <Text variant="bodyMedium" style={styles.muted}>
          Blocks are at <Text style={styles.mono}>#/blocks/&lt;height-or-hash&gt;</Text>, games at{" "}
          <Text style={styles.mono}>#/games/&lt;game-hash&gt;</Text>, accounts at{" "}
          <Text style={styles.mono}>#/accounts/&lt;bech32-address&gt;</Text>.
        </Text>
        <View style={styles.actions}>
          <Button mode="contained" onPress={() => navigate("#/")}>
            Overview
          </Button>
          <Button mode="outlined" onPress={() => navigate("#/blocks")}>
            Blocks
          </Button>
        </View>
      </View>
    </Section>
  );
}

const styles = StyleSheet.create({
  body: { gap: 10, paddingTop: 8 },
  actions: { flexDirection: "row", gap: 8, flexWrap: "wrap" },
  muted: { opacity: 0.72 },
  mono: { fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" },
});
