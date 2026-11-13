/**
 * The block list, plus the small stat card the dashboard uses above it.
 *
 * A row is the block's identity at a glance: height (the link), how long ago it
 * was sealed in chain time, how much work it carried, who sealed it, and the
 * hash. Narrow viewports drop the age and miner columns; the hash column is the
 * one that never goes away, because it is the only globally unique label.
 */

import type { ReactNode } from "react";
import { useWindowDimensions, View } from "react-native";
import { Surface, Text } from "react-native-paper";

import { formatAge, formatInteger, shortAddress } from "../lib/format";
import type { BlockSummary } from "../types";
import { useNow } from "../useResource";
import { EmptyState, HashLink, Link } from "./Chrome";
import { tableStyles as styles } from "./tableStyles";

/** A single number with a caption, for the dashboard's top strip. */
export function StatCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}): ReactNode {
  return (
    <Surface elevation={0} style={styles.stat}>
      <Text variant="labelMedium" style={styles.muted}>
        {label}
      </Text>
      <Text variant="headlineSmall" style={styles.statValue}>
        {value}
      </Text>
      {hint !== undefined && (
        <Text variant="bodySmall" style={styles.muted}>
          {hint}
        </Text>
      )}
    </Surface>
  );
}

function HeaderRow({ wide }: { wide: boolean }): ReactNode {
  return (
    <View style={styles.header}>
      <Text variant="labelSmall" style={[styles.colHeight, styles.muted]}>
        HEIGHT
      </Text>
      {wide && (
        <Text variant="labelSmall" style={[styles.colAge, styles.muted]}>
          AGE
        </Text>
      )}
      <Text variant="labelSmall" style={[styles.colCounts, styles.muted]}>
        TX / PROOFS
      </Text>
      {wide && (
        <Text variant="labelSmall" style={[styles.colMiner, styles.muted]}>
          MINER
        </Text>
      )}
      <Text variant="labelSmall" style={[styles.colHash, styles.muted]}>
        HASH
      </Text>
    </View>
  );
}

/** One block in list form. */
export function BlockRow({ block }: { block: BlockSummary }): ReactNode {
  const { width } = useWindowDimensions();
  const wide = width >= 720;
  const now = useNow(1000);
  return (
    <View style={styles.row}>
      <View style={styles.colHeight}>
        <Link href={`#/blocks/${block.height}`} mono>
          {formatInteger(block.height)}
        </Link>
      </View>
      {wide && (
        <Text variant="bodySmall" style={[styles.colAge, styles.muted]}>
          {formatAge(block.timestamp, now)}
        </Text>
      )}
      <Text variant="bodyMedium" style={styles.colCounts}>
        {block.transactions} / {block.proofs}
      </Text>
      {wide && (
        <View style={styles.colMiner}>
          <Link href={`#/accounts/${block.miner}`} mono>
            {shortAddress(block.miner)}
          </Link>
        </View>
      )}
      <View style={styles.colHash}>
        <HashLink value={block.hash} href={`#/blocks/${block.hash}`} />
      </View>
    </View>
  );
}

/** A block list with a header. */
export function BlockTable({ blocks }: { blocks: BlockSummary[] }): ReactNode {
  const { width } = useWindowDimensions();
  const wide = width >= 720;
  if (blocks.length === 0) return <EmptyState>No blocks yet</EmptyState>;
  return (
    <View>
      <HeaderRow wide={wide} />
      {blocks.map((block) => (
        <BlockRow key={block.hash} block={block} />
      ))}
    </View>
  );
}
