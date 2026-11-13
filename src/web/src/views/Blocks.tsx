/**
 * The block list, paged with the `before_height` cursor the node exposes.
 *
 * Paging is a stack of cursors rather than an offset: the chain can grow or
 * reorganise between two requests, and an offset would silently skip or repeat
 * a block when that happens.
 */

import { useState, type ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import { Button, HelperText, Text, TextInput } from "react-native-paper";

import { fetchBlocks } from "../api";
import { BlockTable } from "../components/BlockTable";
import { EmptyState, FailureNotice, Loading, Section } from "../components/Chrome";
import { formatInteger } from "../lib/format";
import { navigate } from "../router";
import { useResource } from "../useResource";

const PAGE_SIZE = 25;

export function Blocks(): ReactNode {
  const [cursors, setCursors] = useState<Array<number | undefined>>([undefined]);
  const [query, setQuery] = useState("");
  const before = cursors[cursors.length - 1];
  const page = useResource(
    (signal) => fetchBlocks({ limit: PAGE_SIZE, beforeHeight: before, signal }),
    [before],
    before === undefined ? { pollMs: 5000 } : {},
  );

  const blocks = page.data?.blocks ?? [];
  const oldest = blocks[blocks.length - 1];
  const newest = blocks[0];
  const trimmed = query.trim();

  return (
    <Section
      title="Blocks"
      subtitle={
        page.data === undefined
          ? "each block carries its proof and its transactions"
          : `heights ${formatInteger(oldest?.height ?? 0)}–${formatInteger(newest?.height ?? 0)} of ${formatInteger(page.data.tip_height)}`
      }
      action={
        <View style={styles.controls}>
          <Button
            mode="outlined"
            compact
            disabled={cursors.length === 1}
            onPress={() => setCursors((stack) => stack.slice(0, -1))}
          >
            Newer
          </Button>
          <Button
            mode="outlined"
            compact
            disabled={oldest === undefined || oldest.height === 0 || blocks.length < PAGE_SIZE}
            onPress={() => {
              if (oldest === undefined) return;
              setCursors((stack) => [...stack, oldest.height]);
            }}
          >
            Older
          </Button>
        </View>
      }
    >
      <View style={styles.search}>
        <TextInput
          mode="outlined"
          dense
          label="Jump to a height or block hash"
          value={query}
          onChangeText={setQuery}
          autoCapitalize="none"
          autoCorrect={false}
          onSubmitEditing={() => {
            if (trimmed !== "") navigate(`#/blocks/${encodeURIComponent(trimmed)}`);
          }}
          style={styles.searchInput}
        />
        <Button
          mode="contained"
          disabled={trimmed === ""}
          onPress={() => navigate(`#/blocks/${encodeURIComponent(trimmed)}`)}
        >
          Open
        </Button>
      </View>
      <HelperText type="info" visible>
        Heights are links; a hash works too, so a block can be shared straight from the chain.
      </HelperText>

      {page.error !== undefined ? (
        <FailureNotice error={page.error} />
      ) : page.data === undefined ? (
        <Loading />
      ) : page.data.blocks.length === 0 ? (
        <EmptyState>No block at this height</EmptyState>
      ) : (
        <BlockTable blocks={page.data.blocks} />
      )}
      {page.loading && page.data !== undefined && (
        <Text variant="bodySmall" style={styles.muted}>
          refreshing…
        </Text>
      )}
    </Section>
  );
}

const styles = StyleSheet.create({
  controls: { flexDirection: "row", gap: 8 },
  search: { flexDirection: "row", alignItems: "center", gap: 8, marginTop: 8 },
  searchInput: { flexGrow: 1, flexShrink: 1, maxWidth: 520 },
  muted: { opacity: 0.66 },
});
