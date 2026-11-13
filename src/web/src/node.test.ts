/**
 * The rule that keeps one tab from mixing two chains.
 *
 * `/v1/status` polls and carries the network id; `/v1/params` carries the epoch
 * length, the bond and the clock control. If a node is restarted on another
 * chain, the second document is briefly the previous network's, and using it
 * would put every epoch boundary in the wrong place.
 */

import { describe, expect, it } from "vitest";

import { paramsForNetwork } from "./node";
import type { Resource } from "./useResource";

interface Params {
  network_id: string;
  epoch_length: number;
}

const DEVNET: Params = { network_id: "devnet", epoch_length: 1 };
const TESTNET: Params = { network_id: "testnet", epoch_length: 5 };

function resource(data: Params | undefined): Resource<Params> {
  return { data, error: undefined, loading: false, updatedAt: 1, reload: () => undefined };
}

describe("paramsForNetwork", () => {
  it("keeps the parameters that belong to the node's network", () => {
    const params = resource(TESTNET);
    expect(paramsForNetwork(params, "testnet")).toBe(params);
  });

  it("drops the parameters of the network the node has left", () => {
    const params = resource(DEVNET);
    const kept = paramsForNetwork(params, "testnet");
    expect(kept.data).toBeUndefined();
    // Only the document is withheld: the resource keeps its state and reload.
    expect(kept.loading).toBe(false);
    expect(kept.reload).toBe(params.reload);
  });

  it("keeps what it has while the network is still unknown", () => {
    const params = resource(DEVNET);
    expect(paramsForNetwork(params, null)).toBe(params);
  });

  it("has nothing to drop before the first document arrives", () => {
    const params = resource(undefined);
    expect(paramsForNetwork(params, "testnet")).toBe(params);
  });

  it("is why the distinction matters: the epoch of a height depends on it", () => {
    const height = 24;
    expect(Math.floor(height / DEVNET.epoch_length)).toBe(24);
    expect(Math.floor(height / TESTNET.epoch_length)).toBe(4);
  });
});
