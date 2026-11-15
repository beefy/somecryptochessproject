/**
 * Wallet preferences: nothing secret, and nothing that belongs in the vault.
 *
 * The auto-lock timeout and the last-used address are conveniences, not key
 * material, so they live in `localStorage` where they survive a tab close. Every
 * access is wrapped: a browser in a private mode can throw on `localStorage`.
 */

const AUTO_LOCK_KEY = "sccp.wallet.autoLockSeconds";
const LAST_ADDRESS_KEY = "sccp.wallet.lastAddress";

/** Five minutes of keyboard silence is enough to walk away from an open tab. */
export const DEFAULT_AUTO_LOCK_SECONDS = 300;

/** The timeouts the settings view offers, in seconds. */
export const AUTO_LOCK_CHOICES: readonly number[] = [60, 300, 900, 1800];

export function readAutoLockSeconds(): number {
  try {
    const stored = Number(window.localStorage.getItem(AUTO_LOCK_KEY));
    if (AUTO_LOCK_CHOICES.includes(stored)) return stored;
  } catch {
    // Storage disabled: fall through to the default rather than fail the view.
  }
  return DEFAULT_AUTO_LOCK_SECONDS;
}

export function writeAutoLockSeconds(seconds: number): void {
  try {
    window.localStorage.setItem(AUTO_LOCK_KEY, String(seconds));
  } catch {
    // A preference that cannot be stored is not worth an error dialog.
  }
}

export function readLastAddress(): string | null {
  try {
    return window.localStorage.getItem(LAST_ADDRESS_KEY);
  } catch {
    return null;
  }
}

export function writeLastAddress(address: string | null): void {
  try {
    if (address === null) window.localStorage.removeItem(LAST_ADDRESS_KEY);
    else window.localStorage.setItem(LAST_ADDRESS_KEY, address);
  } catch {
    // Ignored for the same reason as above.
  }
}
