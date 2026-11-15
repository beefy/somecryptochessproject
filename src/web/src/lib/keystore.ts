/**
 * Keyfiles: scrypt + AES-256-GCM, the same file `sccp wallet new` writes.
 *
 * The browser and the CLI have to open each other's keyfiles, so this is a port
 * of `sccp/crypto/wallet.py`, not a second format:
 *
 * ```
 * {
 *   "version": 2, "scheme": "ed25519-mldsa65", "network": "testnet",
 *   "address": "sccpt1...", "public_key": "<3970 hex>",
 *   "kdf": {"name": "scrypt", "n": 131072, "r": 8, "p": 1, "salt": "<32 hex>"},
 *   "cipher": {"name": "aes-256-gcm", "nonce": "<24 hex>"},
 *   "created_at": 1763200000, "ciphertext": "<96 hex>"
 * }
 * ```
 *
 * The associated data for AES-GCM is the canonical JSON of every field except
 * `ciphertext`, so editing the network, address or KDF parameters fails
 * authentication instead of decrypting to a different wallet. The stored secret
 * is the 32-byte master seed both key halves derive from.
 *
 * scrypt runs through `@noble/hashes` and AES-GCM through WebCrypto. Nothing here
 * touches storage: unlocking returns a keypair to the caller, and the document is
 * the caller's to keep encrypted.
 */

import { scryptAsync } from "@noble/hashes/scrypt.js";

import { addressHrp } from "./bech32";
import { canonicalJson, fromHex, toHex } from "./canonical";
import {
  HYBRID_ALGORITHM,
  HYBRID_PUBLIC_KEY_HEX_LENGTH,
  type HybridKeypair,
  addressFor,
  generateHybridKeypair,
  hybridKeypairFromSeed,
  hybridPublicKeyHex,
} from "./keys";

export const KEYFILE_VERSION = 2;
export const KEYFILE_SCHEME = HYBRID_ALGORITHM;
export const KDF_NAME = "scrypt";
export const CIPHER_NAME = "aes-256-gcm";

/** Production scrypt cost (`DEFAULT_KDF` in Python): 128 MiB per guess. */
export const SCRYPT_N = 2 ** 17;
export const SCRYPT_R = 8;
export const SCRYPT_P = 1;

export const SCRYPT_SALT_SIZE = 16;
export const AES_NONCE_SIZE = 12;
export const KEY_SIZE = 32;

/** A usability floor, not an entropy proof (the CLI enforces the same one). */
export const MIN_PASSPHRASE_CHARS = 12;

/** Networks a keyfile may name; the address prefix has to agree with it. */
export const KNOWN_NETWORKS: readonly string[] = ["mainnet", "testnet", "devnet"];

export class KeyfileError extends Error {
  override name = "KeyfileError";
}

export interface KdfParams {
  name: typeof KDF_NAME;
  n: number;
  r: number;
  p: number;
  /** 16-byte salt, lowercase hex. */
  salt: string;
}

export interface CipherParams {
  name: typeof CIPHER_NAME;
  /** 12-byte nonce, lowercase hex. */
  nonce: string;
}

/** The public half of a keyfile plus the encrypted seed. */
export interface KeyfileDocument {
  version: typeof KEYFILE_VERSION;
  scheme: typeof KEYFILE_SCHEME;
  network: string;
  address: string;
  public_key: string;
  kdf: KdfParams;
  cipher: CipherParams;
  created_at: number;
  ciphertext: string;
}

/** Everything that is authenticated but not encrypted: the AES-GCM AAD. */
type KeyfileHeader = Omit<KeyfileDocument, "ciphertext">;


const HEX = /^[0-9a-f]+$/;

function requireString(document: Record<string, unknown>, key: string): string {
  const value = document[key];
  if (typeof value !== "string") throw new KeyfileError(`keyfile field "${key}" must be a string`);
  return value;
}

function requireInteger(document: Record<string, unknown>, key: string): number {
  const value = document[key];
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    throw new KeyfileError(`keyfile field "${key}" must be an integer`);
  }
  return value;
}

function requireMapping(document: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = document[key];
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new KeyfileError(`keyfile field "${key}" must be an object`);
  }
  return value as Record<string, unknown>;
}

function requireHex(document: Record<string, unknown>, key: string, size: number): string {
  const text = requireString(document, key).toLowerCase();
  if (!HEX.test(text) || text.length !== size * 2) {
    throw new KeyfileError(`keyfile field "${key}" must be ${size} bytes of hex`);
  }
  return text;
}

/** The authenticated header, with exactly the fields the CLI writes. */
function header(document: KeyfileDocument): KeyfileHeader {
  return {
    version: document.version,
    scheme: document.scheme,
    network: document.network,
    address: document.address,
    public_key: document.public_key,
    kdf: {
      name: document.kdf.name,
      n: document.kdf.n,
      r: document.kdf.r,
      p: document.kdf.p,
      salt: document.kdf.salt,
    },
    cipher: { name: document.cipher.name, nonce: document.cipher.nonce },
    created_at: document.created_at,
  };
}

/**
 * Parse and validate a keyfile, from text or an already-parsed object.
 *
 * The checks mirror `KeyfileHeader.from_dict`: one version, one scheme, a known
 * network, a hybrid-length public key, the scrypt/`aes-256-gcm` names and the
 * exact salt, nonce and ciphertext sizes. The address is only prefix-checked
 * here -- it cannot be *derived* until the passphrase decrypts the seed, and
 * unlocking performs that stronger check.
 */
export function parseKeyfile(input: string | unknown): KeyfileDocument {
  let raw: unknown = input;
  if (typeof input === "string") {
    try {
      raw = JSON.parse(input);
    } catch {
      throw new KeyfileError("the keyfile is not valid JSON");
    }
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new KeyfileError("a keyfile is a JSON object");
  }
  const document = raw as Record<string, unknown>;

  const version = requireInteger(document, "version");
  if (version !== KEYFILE_VERSION) {
    throw new KeyfileError(
      `unsupported keyfile version ${version}: only ${KEYFILE_VERSION} (hybrid) is accepted`,
    );
  }
  const scheme = requireString(document, "scheme");
  if (scheme !== KEYFILE_SCHEME) {
    throw new KeyfileError(`unsupported keyfile scheme "${scheme}" (only "${KEYFILE_SCHEME}")`);
  }
  const network = requireString(document, "network");
  if (!KNOWN_NETWORKS.includes(network)) {
    throw new KeyfileError(
      `keyfile names unknown network "${network}" (known: ${KNOWN_NETWORKS.join(", ")})`,
    );
  }
  const address = requireString(document, "address");
  const prefix = `${addressHrp(network)}1`;
  if (!address.startsWith(prefix) || address.length > 90) {
    throw new KeyfileError(`keyfile address "${address}" is not a ${network} address`);
  }
  const publicKey = requireString(document, "public_key").toLowerCase();
  if (!HEX.test(publicKey) || publicKey.length !== HYBRID_PUBLIC_KEY_HEX_LENGTH) {
    throw new KeyfileError(
      `keyfile public_key must be ${HYBRID_PUBLIC_KEY_HEX_LENGTH} hex characters`,
    );
  }

  const kdfDocument = requireMapping(document, "kdf");
  const kdfName = requireString(kdfDocument, "name");
  if (kdfName !== KDF_NAME) throw new KeyfileError(`unsupported keyfile kdf "${kdfName}"`);
  const n = requireInteger(kdfDocument, "n");
  const r = requireInteger(kdfDocument, "r");
  const p = requireInteger(kdfDocument, "p");
  if (n < 2 || (n & (n - 1)) !== 0) {
    throw new KeyfileError("scrypt n must be a power of two >= 2");
  }
  if (r < 1 || p < 1) throw new KeyfileError("scrypt r and p must be positive");
  const salt = requireHex(kdfDocument, "salt", SCRYPT_SALT_SIZE);

  const cipherDocument = requireMapping(document, "cipher");
  const cipherName = requireString(cipherDocument, "name");
  if (cipherName !== CIPHER_NAME) {
    throw new KeyfileError(`unsupported keyfile cipher "${cipherName}"`);
  }
  const nonce = requireHex(cipherDocument, "nonce", AES_NONCE_SIZE);

  const ciphertext = requireString(document, "ciphertext").toLowerCase();
  if (!HEX.test(ciphertext) || ciphertext.length < (KEY_SIZE + 16) * 2) {
    throw new KeyfileError("keyfile ciphertext is not a hex AES-GCM payload");
  }

  return {
    version: KEYFILE_VERSION,
    scheme: KEYFILE_SCHEME,
    network,
    address,
    public_key: publicKey,
    kdf: { name: KDF_NAME, n, r, p, salt },
    cipher: { name: CIPHER_NAME, nonce },
    created_at: requireInteger(document, "created_at"),
    ciphertext,
  };
}


/** `JSON.stringify` with sorted keys and two-space indent, like `json.dumps`. */
function sortedJson(value: unknown, indent = 0): string {
  const pad = "  ".repeat(indent);
  const inner = pad + "  ";
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    return `[\n${value.map((item) => `${inner}${sortedJson(item, indent + 1)}`).join(",\n")}\n${pad}]`;
  }
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) =>
      a < b ? -1 : a > b ? 1 : 0,
    );
    if (entries.length === 0) return "{}";
    const members = entries.map(
      ([key, item]) => `${inner}${JSON.stringify(key)}: ${sortedJson(item, indent + 1)}`,
    );
    return `{\n${members.join(",\n")}\n${pad}}`;
  }
  return JSON.stringify(value);
}

/**
 * The keyfile as text, byte-compatible with `sccp wallet new`.
 *
 * The CLI writes `json.dumps(document, indent=2, sort_keys=True) + "\n"`, so an
 * export from the browser diffs cleanly against one from the CLI.
 */
export function keyfileText(document: KeyfileDocument): string {
  return `${sortedJson(document)}\n`;
}

function passphraseBytes(passphrase: string): Uint8Array {
  return new TextEncoder().encode(passphrase.normalize("NFKD"));
}

/** Derive the 32-byte AES key with the parameters the header records. */
async function deriveKey(passphrase: string, kdf: KdfParams): Promise<Uint8Array> {
  const key = await scryptAsync(passphraseBytes(passphrase), fromHex(kdf.salt), {
    N: kdf.n,
    r: kdf.r,
    p: kdf.p,
    dkLen: KEY_SIZE,
  });
  return new Uint8Array(key);
}

async function importAesKey(raw: Uint8Array, usage: KeyUsage): Promise<CryptoKey> {
  return crypto.subtle.importKey("raw", new Uint8Array(raw), { name: "AES-GCM" }, false, [usage]);
}

/** Decrypt the stored seed; a bad tag means a wrong passphrase, never a retry. */
async function decryptSeed(document: KeyfileDocument, passphrase: string): Promise<Uint8Array> {
  const key = await deriveKey(passphrase, document.kdf);
  const aad = new TextEncoder().encode(canonicalJson(header(document)));
  let plain: ArrayBuffer;
  try {
    plain = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: new Uint8Array(fromHex(document.cipher.nonce)), additionalData: aad },
      await importAesKey(key, "decrypt"),
      new Uint8Array(fromHex(document.ciphertext)),
    );
  } catch {
    throw new KeyfileError(
      `cannot decrypt the keyfile for ${document.address}: wrong passphrase or corrupted ciphertext`,
    );
  }
  const seed = new Uint8Array(plain);
  if (seed.length !== KEY_SIZE) {
    throw new KeyfileError(`the keyfile decrypted to ${seed.length} bytes, expected ${KEY_SIZE}`);
  }
  return seed;
}

/**
 * Unlock a keyfile: scrypt, AES-GCM, then check the derived key against the header.
 *
 * The header is authenticated, but this second check catches a keyfile whose
 * public fields were replaced before encryption -- signing with a key that is not
 * the one the file names is not something a caller should discover on the wire.
 */
export async function unlockKeyfile(
  input: KeyfileDocument | string,
  passphrase: string,
): Promise<HybridKeypair> {
  const document = parseKeyfile(input);
  const seed = await decryptSeed(document, passphrase);
  const keypair = hybridKeypairFromSeed(seed);
  if (
    hybridPublicKeyHex(keypair) !== document.public_key ||
    addressFor(keypair.publicKey, document.network) !== document.address
  ) {
    throw new KeyfileError(
      "the keyfile's header does not describe the key it holds; it may have been edited",
    );
  }
  return keypair;
}

/** A fresh scrypt salt: 16 bytes, unique per keyfile. */
export function randomSalt(): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(SCRYPT_SALT_SIZE));
}

/** A fresh AES-GCM nonce: 12 bytes, never reused under one key. */
export function randomNonce(): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(AES_NONCE_SIZE));
}

export interface CreateKeyfileOptions {
  network: string;
  passphrase: string;
  /** 32-byte master seed; a fresh one from the CSPRNG when omitted. */
  seed?: Uint8Array;
  /** scrypt cost; the production default unless a test pins a cheap one. */
  kdf?: { n?: number; r?: number; p?: number };
  createdAt?: number;
  salt?: Uint8Array;
  nonce?: Uint8Array;
}

/**
 * Create a keyfile: derive both key halves, encrypt the seed, return the document.
 *
 * The parameters that would normally be random are injectable so a test can pin
 * them against the vector the CLI produced; nothing in the wallet does.
 */
export async function createKeyfile(
  options: CreateKeyfileOptions,
): Promise<{ document: KeyfileDocument; keypair: HybridKeypair }> {
  if (!KNOWN_NETWORKS.includes(options.network)) {
    throw new KeyfileError(`cannot create a keyfile on unknown network "${options.network}"`);
  }
  if (options.passphrase.length < MIN_PASSPHRASE_CHARS) {
    throw new KeyfileError(
      `a keyfile passphrase must be at least ${MIN_PASSPHRASE_CHARS} characters`,
    );
  }
  const keypair =
    options.seed === undefined ? generateHybridKeypair() : hybridKeypairFromSeed(options.seed);
  const kdf: KdfParams = {
    name: KDF_NAME,
    n: options.kdf?.n ?? SCRYPT_N,
    r: options.kdf?.r ?? SCRYPT_R,
    p: options.kdf?.p ?? SCRYPT_P,
    salt: toHex(options.salt ?? randomSalt()),
  };
  const nonce = toHex(options.nonce ?? randomNonce());
  const document: KeyfileDocument = {
    version: KEYFILE_VERSION,
    scheme: KEYFILE_SCHEME,
    network: options.network,
    address: addressFor(keypair.publicKey, options.network),
    public_key: hybridPublicKeyHex(keypair),
    kdf,
    cipher: { name: CIPHER_NAME, nonce },
    created_at: options.createdAt ?? Math.floor(Date.now() / 1000),
    ciphertext: "",
  };

  const key = await deriveKey(options.passphrase, kdf);
  const aad = new TextEncoder().encode(canonicalJson(header(document)));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: new Uint8Array(fromHex(nonce)), additionalData: aad },
    await importAesKey(key, "encrypt"),
    new Uint8Array(keypair.seed),
  );
  document.ciphertext = toHex(new Uint8Array(ciphertext));
  return { document, keypair };
}
