/**
 * Resolve whatever the explorer's search box was given.
 *
 * A height is unambiguous. A 32-byte hash is not: block hashes and txids have
 * the same shape, so the only authority is the node. Ask for the block first,
 * then the transaction, and let a transport failure surface instead of turning
 * it into "not found".
 */

import { fetchBlock, fetchTransaction, NotFoundError } from "../api";
import type { Route } from "../router";

/** A canonical 32-byte hex reference; the node stores these lowercase. */
const HEX_32_BYTES = /^[0-9a-fA-F]{64}$/;
const HEIGHT = /^\d+$/;

/**
 * The route the search box should open, or `null` when neither a block nor a
 * transaction matches. The caller decides how to present a miss; this helper
 * does not invent a route that is known to 404.
 */
export async function resolveSearchTarget(
  raw: string,
  signal?: AbortSignal,
): Promise<Route | null> {
  const query = raw.trim();
  if (query === "") return null;

  if (HEIGHT.test(query)) return { name: "block", reference: query };

  // Hex references are case-insensitive to a person pasting one, even though
  // the chain spells every digest in lowercase.
  const reference = HEX_32_BYTES.test(query) ? query.toLowerCase() : query;

  try {
    await fetchBlock(reference, signal);
    return { name: "block", reference };
  } catch (error) {
    if (!(error instanceof NotFoundError)) throw error;
  }

  try {
    const record = await fetchTransaction(reference, signal);
    return { name: "transaction", txid: record.txid };
  } catch (error) {
    if (error instanceof NotFoundError) return null;
    throw error;
  }
}
