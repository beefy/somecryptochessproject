/**
 * Nicknames are local labels, so the rules worth pinning are the tidy ones:
 * whitespace collapses, an empty label forgets, one address has one nickname,
 * and a corrupted store reads as empty instead of breaking a view.
 */

import { beforeEach, describe, expect, it } from "vitest";

import {
  CONTACT_LABEL_MAX,
  forgetContact,
  labelFor,
  parseContacts,
  readContacts,
  saveContact,
  upsertContact,
  type Contact,
} from "./contacts";

function memoryStorage(): Storage {
  const items = new Map<string, string>();
  return {
    get length() {
      return items.size;
    },
    clear: () => items.clear(),
    getItem: (key: string) => items.get(key) ?? null,
    key: (index: number) => [...items.keys()][index] ?? null,
    removeItem: (key: string) => {
      items.delete(key);
    },
    setItem: (key: string, value: string) => {
      items.set(key, value);
    },
  } as Storage;
}

beforeEach(() => {
  Object.defineProperty(globalThis, "localStorage", {
    value: memoryStorage(),
    configurable: true,
  });
});

const ALICE: Contact = {
  address: "sccpt1qalice",
  network: "testnet",
  label: "Alice",
  updatedAt: 1,
};

describe("upsertContact", () => {
  it("adds, then replaces the nickname for the same address", () => {
    const added = upsertContact([], ALICE);
    expect(added).toEqual([ALICE]);
    const replaced = upsertContact(added, { ...ALICE, label: "Alice (miner)" });
    expect(replaced).toEqual([{ ...ALICE, label: "Alice (miner)" }]);
  });

  it("collapses whitespace and enforces the length limit", () => {
    const [contact] = upsertContact([], { ...ALICE, label: `  Alice   the\nminer ${"x".repeat(80)}` });
    expect(contact?.label.startsWith("Alice the miner x")).toBe(true);
    expect(contact?.label.length).toBe(CONTACT_LABEL_MAX);
  });

  it("forgets the contact when the label is empty", () => {
    expect(upsertContact([ALICE], { ...ALICE, label: "   " })).toEqual([]);
  });

  it("orders by label", () => {
    const contacts = upsertContact(upsertContact([], ALICE), {
      ...ALICE,
      address: "sccpt1qbob",
      label: "Bob",
    });
    expect(contacts.map((contact) => contact.label)).toEqual(["Alice", "Bob"]);
  });
});

describe("labelFor", () => {
  it("finds a nickname and tolerates a missing address", () => {
    expect(labelFor([ALICE], ALICE.address)).toBe("Alice");
    expect(labelFor([ALICE], "sccpt1qunknown")).toBeNull();
    expect(labelFor([ALICE], null)).toBeNull();
  });
});

describe("the stored book", () => {
  it("reads junk as empty", () => {
    expect(parseContacts("not json")).toEqual([]);
    expect(parseContacts("{}")).toEqual([]);
    expect(parseContacts('[{"address":"sccpt1qx"}]')).toEqual([]);
  });

  it("round-trips through localStorage and forgets", () => {
    expect(readContacts()).toEqual([]);
    saveContact(ALICE.address, ALICE.network, "  Alice ");
    expect(readContacts()).toEqual([{ ...ALICE, updatedAt: expect.any(Number) }]);
    forgetContact(ALICE.address);
    expect(readContacts()).toEqual([]);
  });
});
