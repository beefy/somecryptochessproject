/**
 * Column widths shared by the block and game lists.
 *
 * Narrow viewports drop columns rather than squeezing them; the widths live here
 * so both tables keep the same rhythm down the page. The `scroll*` and
 * `tableNarrow` styles are what keeps an unavoidable width from pushing the
 * whole page sideways on a phone: the table scrolls inside its own box.
 */

import { Platform, StyleSheet } from "react-native";

export const tableStyles = StyleSheet.create({
  scroll: { width: "100%" },
  scrollContent: { minWidth: "100%" },
  table: { flexGrow: 1 },
  /** The floor a narrow table scrolls to instead of shrinking past legibility. */
  tableNarrow: { minWidth: 460 },
  stat: { flexGrow: 1, flexBasis: 150, padding: 12, borderRadius: 12, gap: 2 },
  statValue: { fontVariant: ["tabular-nums"] },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingVertical: 6,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(128,128,128,0.4)",
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingVertical: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(128,128,128,0.24)",
  },
  colHeight: { width: 76 },
  colAge: { width: 96 },
  colCounts: { width: 112, fontVariant: ["tabular-nums"] },
  colMiner: { flexGrow: 1, flexShrink: 1, minWidth: 120 },
  colHash: { width: 132, alignItems: "flex-end" },
  colResult: { width: 64 },
  colVariant: { width: 62 },
  colSeats: { flexGrow: 1, flexShrink: 1, minWidth: 140, gap: 2 },
  colPlies: { width: 72 },
  colEpoch: { width: 72 },
  colWhere: { width: 148, flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
  colGameHash: { width: 132, alignItems: "flex-end" },
  muted: { opacity: 0.66 },
  numeric: { fontVariant: ["tabular-nums"] },
  hashText: { fontSize: Platform.OS === "web" ? 12 : 11 },
});
