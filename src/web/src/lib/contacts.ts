/**
 * Nicknames for addresses: the wallets in this browser and the people it sends to.
 *
 * A nickname is a convenience, never evidence. It is not in the keyfile, not on
 * chain and not covered by a signature -- it is a label for a human. That is why
 * it lives in `localStorage` beside the auto-lock preference instead of in the
 * vault: if the browser forgets it, every screen falls back to the bech32
 * address, which is the thing that actually identifies the account.
 *
 * The store is one JSON array keyed by address (an address embeds its network
 * prefix, so one address is one contact). Anything malformed reads as an empty
 * book rather than an exception: a nickname is never worth failing a view over.
 */

export interface Contact {
  address: string;
  network: string;
  label: string;
  updatedAt: number;
}

const CONTACTS_KEY = "sccp.wallet.contacts";

/** Longest nickname the forms accept; a label is a hint, not a paragraph. */
export const CONTACT_LABEL_MAX = 40;

/** Collapse whitespace, trim, and cut to the limit. */
function clean(label: string): string {
  return label.trim().replace(/\s+/g, " ").slice(0, CONTACT_LABEL_MAX);
}

function isContact(value: unknown): value is Contact {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.address === "string" &&
    typeof candidate.network === "string" &&
    typeof candidate.label === "string" &&
    typeof candidate.updatedAt === "number"
  );
}

function sorted(contacts: readonly Contact[]): Contact[] {
  return [...contacts].sort(
    (left, right) =>
      left.label.localeCompare(right.label) || left.address.localeCompare(right.address),
  );
}

/** Parse the stored JSON; junk entries are dropped one by one. */
export function parseContacts(text: string | null): Contact[] {
  if (text === null) return [];
  try {
    const parsed: unknown = JSON.parse(text);
    if (!Array.isArray(parsed)) return [];
    return sorted(
      parsed
        .filter(isContact)
        .map((contact) => ({ ...contact, label: clean(contact.label) }))
        .filter((contact) => contact.label !== ""),
    );
  } catch {
    return [];
  }
}

/**
 * Add or replace one nickname.
 *
 * An empty label forgets the contact instead of storing a blank row, so the
 * "Clear" button and the "Save" button are the same code path.
 */
export function upsertContact(contacts: readonly Contact[], contact: Contact): Contact[] {
  const label = clean(contact.label);
  const without = contacts.filter((entry) => entry.address !== contact.address);
  if (label === "") return sorted(without);
  return sorted([...without, { ...contact, label }]);
}

/** The nickname for `address`, or `null` when it has none. */
export function labelFor(
  contacts: readonly Contact[],
  address: string | null | undefined,
): string | null {
  if (address === null || address === undefined) return null;
  return contacts.find((contact) => contact.address === address)?.label ?? null;
}

function storage(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    // A private mode can throw on the getter itself.
    return null;
  }
}

function writeContacts(contacts: readonly Contact[]): void {
  const store = storage();
  if (store === null) return;
  try {
    store.setItem(CONTACTS_KEY, JSON.stringify(contacts));
  } catch {
    // A nickname that cannot be stored is not worth an error dialog.
  }
}

/** Every nickname this browser holds, ordered by label. */
export function readContacts(): Contact[] {
  const store = storage();
  if (store === null) return [];
  try {
    return parseContacts(store.getItem(CONTACTS_KEY));
  } catch {
    return [];
  }
}

/** Save (or clear, with an empty label) one nickname and return the new list. */
export function saveContact(address: string, network: string, label: string): Contact[] {
  const contacts = upsertContact(readContacts(), {
    address,
    network,
    label,
    updatedAt: Date.now(),
  });
  writeContacts(contacts);
  return contacts;
}

/** Remove one nickname and return the new list. */
export function forgetContact(address: string): Contact[] {
  const contacts = readContacts().filter((contact) => contact.address !== address);
  writeContacts(contacts);
  return contacts;
}
