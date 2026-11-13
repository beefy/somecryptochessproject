/**
 * Shared shell pieces: page width, sections, key/value rows, hashes and states.
 *
 * Everything is a React Native primitive (`View`, `Text`, `Pressable`), which
 * `react-native-web` renders to DOM, so these would run unchanged on a phone.
 */

import type { ReactNode } from "react";
import { useMemo } from "react";
import { Pressable, ScrollView, StyleSheet, useWindowDimensions, View } from "react-native";
import { ActivityIndicator, Chip, Text } from "react-native-paper";

import { copyText, shortHash } from "../lib/format";
import { navigate } from "../router";
import { monospace } from "../theme";

/** Page width: a readable column on desktop, full width on a phone. */
export function Page({ children }: { children: ReactNode }): ReactNode {
  const { width } = useWindowDimensions();
  const narrow = width < 720;
  return (
    <ScrollView style={styles.scroll} contentContainerStyle={[styles.page, narrow && styles.pageNarrow]}>
      {children}
    </ScrollView>
  );
}

/** A titled section with an optional action on the right. */
export function Section({
  title,
  subtitle,
  action,
  children,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
}): ReactNode {
  return (
    <View style={styles.section}>
      <View style={styles.sectionHeader}>
        <View style={styles.sectionTitles}>
          <Text variant="titleMedium">{title}</Text>
          {subtitle !== undefined && (
            <Text variant="bodySmall" style={styles.muted}>
              {subtitle}
            </Text>
          )}
        </View>
        {action}
      </View>
      {children}
    </View>
  );
}

/** One label/value line; `mono` is for hashes, keys and signatures. */
export function KeyValue({
  label,
  children,
  mono = false,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
}): ReactNode {
  return (
    <View style={styles.row}>
      <Text variant="bodySmall" style={[styles.rowLabel, styles.muted]}>
        {label}
      </Text>
      <View style={styles.rowValue}>
        {typeof children === "string" || typeof children === "number" ? (
          <Text variant="bodyMedium" selectable style={mono ? styles.mono : undefined}>
            {String(children)}
          </Text>
        ) : (
          children
        )}
      </View>
    </View>
  );
}

/** Monospace, selectable text for anything that is hex; tap to copy. */
export function Mono({ children, copy = false }: { children: string; copy?: boolean }): ReactNode {
  const label = (
    <Text variant="bodyMedium" selectable style={styles.mono}>
      {children}
    </Text>
  );
  if (!copy) return label;
  return (
    <Pressable onPress={() => void copyText(children)} style={styles.inlinePressable}>
      {label}
    </Pressable>
  );
}

/** An in-app link that is also a real anchor, so copy-link and middle-click work. */
export function Link({
  href,
  children,
  mono = false,
}: {
  href: string;
  children: ReactNode;
  mono?: boolean;
}): ReactNode {
  return (
    <Pressable href={href} onPress={() => navigate(href)} style={styles.inlinePressable}>
      <Text variant="bodyMedium" style={[styles.link, mono && styles.mono]}>
        {children}
      </Text>
    </Pressable>
  );
}

/** A hash rendered as a link, shortened unless `full`. */
export function HashLink({
  value,
  href,
  full = false,
}: {
  value: string;
  href?: string;
  full?: boolean;
}): ReactNode {
  const label = full ? value : shortHash(value, 10);
  return href === undefined ? <Mono copy>{label}</Mono> : <Link href={href} mono>{label}</Link>;
}

/** A coloured pill for a state (settled, pending, upheld, ...). */
export function StatusPill({
  label,
  tone = "neutral",
}: {
  label: string;
  tone?: "neutral" | "good" | "warn" | "bad" | "info";
}): ReactNode {
  const colour = useMemo(() => {
    switch (tone) {
      case "good":
        return "#2e7d32";
      case "warn":
        return "#b26a00";
      case "bad":
        return "#b3261e";
      case "info":
        return "#00639b";
      default:
        return "#5f6368";
    }
  }, [tone]);
  return (
    <Chip compact mode="outlined" style={[styles.pill, { borderColor: colour }]} textStyle={[styles.pillText, { color: colour }]}>
      {label}
    </Chip>
  );
}

/** A quiet placeholder for an empty list. */
export function EmptyState({ children }: { children: string }): ReactNode {
  return (
    <View style={styles.empty}>
      <Text variant="bodyMedium" style={styles.muted}>
        {children}
      </Text>
    </View>
  );
}

/** A failure notice: the node is unreachable, or the request was rejected. */
export function FailureNotice({ error }: { error: Error }): ReactNode {
  return (
    <View style={[styles.notice, styles.noticeBad]}>
      <Text variant="bodyMedium" style={styles.noticeTitle}>
        {error.message}
      </Text>
      <Text variant="bodySmall" style={styles.muted}>
        Is the node running? Start one with <Text style={styles.mono}>sccp serve --web</Text>, or point{" "}
        <Text style={styles.mono}>VITE_SCCP_API</Text> at a node URL.
      </Text>
    </View>
  );
}

/** A centred spinner for a view with nothing to show yet. */
export function Loading(): ReactNode {
  return (
    <View style={styles.empty}>
      <ActivityIndicator />
    </View>
  );
}

/** A preformatted, horizontally scrollable JSON/PGN block. */
export function CodeBlock({ children }: { children: string }): ReactNode {
  return (
    <ScrollView horizontal style={styles.codeBlock}>
      <Text selectable style={[styles.mono, styles.codeText]}>
        {children}
      </Text>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  scroll: { flex: 1 },
  page: {
    width: "100%",
    maxWidth: 1080,
    alignSelf: "center",
    paddingHorizontal: 20,
    paddingVertical: 16,
    gap: 16,
  },
  pageNarrow: { paddingHorizontal: 12 },
  section: { gap: 8 },
  sectionHeader: {
    flexDirection: "row",
    alignItems: "flex-end",
    justifyContent: "space-between",
    gap: 12,
    flexWrap: "wrap",
  },
  sectionTitles: { gap: 2, flexShrink: 1 },
  muted: { opacity: 0.66 },
  mono: { fontFamily: monospace, fontSize: 13 },
  row: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 12,
    paddingVertical: 6,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(128,128,128,0.32)",
    flexWrap: "wrap",
  },
  rowLabel: { width: 168 },
  rowValue: { flexShrink: 1, flexGrow: 1, minWidth: 200 },
  inlinePressable: { flexShrink: 1 },
  link: { textDecorationLine: "underline" },
  pill: { backgroundColor: "transparent" },
  pillText: { fontSize: 12 },
  empty: { paddingVertical: 28, alignItems: "center" },
  notice: { padding: 12, borderRadius: 12, gap: 6 },
  noticeBad: { backgroundColor: "rgba(179,38,30,0.12)" },
  noticeTitle: { fontWeight: "600" },
  codeBlock: {
    backgroundColor: "rgba(128,128,128,0.12)",
    borderRadius: 12,
    padding: 12,
    maxHeight: 420,
  },
  codeText: { fontSize: 12, lineHeight: 18 },
});

