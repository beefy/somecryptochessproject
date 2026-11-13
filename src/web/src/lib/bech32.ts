/**
 * BIP-173 bech32, the only encoding SCCP addresses use.
 *
 * `sccp/crypto/address.py` encodes a 21-byte program (version byte + 20-byte
 * hash) with the *primitives*, not the segwit helpers, because those hard-code
 * witness lengths. This is the same subset: polymod, hrp expansion, convertbits
 * and the checksum, written out so a browser can derive an address from a
 * public key without shipping a bech32 dependency. A version-1 (hybrid) program
 * is 33 bytes and is not encoded here; see {@link HYBRID_ADDRESS_VERSION}.
 */

const CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";

const GENERATOR = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];

/** Program version byte for Ed25519 keys (`ADDRESS_VERSION` in Python). */
export const ADDRESS_VERSION = 0;

/**
 * Program version byte for hybrid Ed25519 + ML-DSA-65 keys
 * (`HYBRID_ADDRESS_VERSION` in Python).
 *
 * `encodeProgram` refuses it: an ML-DSA-65 signature is 3309 bytes and no
 * browser primitive signs one here, so a page that cannot make a valid hybrid
 * signature must not hand out a hybrid address either. The CLI wallet owns that
 * scheme, and this module fails closed instead of writing a version-1 program it
 * cannot back with a signature.
 */
export const HYBRID_ADDRESS_VERSION = 1;

/** Address hash length in bytes. */
export const HASH_SIZE = 20;

export class Bech32Error extends Error {
  override name = "Bech32Error";
}

function polymod(values: readonly number[]): number {
  let checksum = 1;
  for (const value of values) {
    const top = checksum >>> 25;
    checksum = ((checksum & 0x1ffffff) << 5) ^ value;
    for (let bit = 0; bit < 5; bit += 1) {
      if (((top >>> bit) & 1) === 1) {
        checksum ^= GENERATOR[bit] as number;
      }
    }
  }
  return checksum >>> 0;
}

function hrpExpand(hrp: string): number[] {
  const out: number[] = [];
  for (const char of hrp) out.push(char.charCodeAt(0) >> 5);
  out.push(0);
  for (const char of hrp) out.push(char.charCodeAt(0) & 31);
  return out;
}

/**
 * Regroup `data` from `from`-bit to `to`-bit units (BIP-173 `convertbits`).
 *
 * Returns `null` for the padding cases Python rejects, so a caller cannot
 * silently produce an address from a malformed program.
 */
export function convertBits(
  data: ArrayLike<number>,
  from: number,
  to: number,
  pad: boolean,
): number[] | null {
  let accumulator = 0;
  let bits = 0;
  const out: number[] = [];
  const maxValue = (1 << to) - 1;
  const maxAccumulator = (1 << (from + to - 1)) - 1;
  for (let index = 0; index < data.length; index += 1) {
    const value = data[index] as number;
    if (value < 0 || value >> from !== 0) return null;
    accumulator = ((accumulator << from) | value) & maxAccumulator;
    bits += from;
    while (bits >= to) {
      bits -= to;
      out.push((accumulator >> bits) & maxValue);
    }
  }
  if (pad) {
    if (bits > 0) out.push((accumulator << (to - bits)) & maxValue);
  } else if (bits >= from || ((accumulator << (to - bits)) & maxValue) !== 0) {
    return null;
  }
  return out;
}

/** Encode 5-bit `words` under `hrp` with the bech32 checksum. */
export function bech32Encode(hrp: string, words: readonly number[]): string {
  const values = [...hrpExpand(hrp), ...words];
  for (let index = 0; index < 6; index += 1) values.push(0);
  const checksum = polymod(values) ^ 1;
  let out = `${hrp}1`;
  for (const word of words) {
    if (word < 0 || word > 31) throw new Bech32Error(`word ${word} is not 5 bits`);
    out += CHARSET[word];
  }
  for (let index = 0; index < 6; index += 1) {
    out += CHARSET[(checksum >>> (5 * (5 - index))) & 31];
  }
  return out;
}

/** Encode a raw 21-byte address program as a lowercase bech32 address. */
export function encodeProgram(hrp: string, program: Uint8Array): string {
  if (program.length !== 1 + HASH_SIZE) {
    throw new Bech32Error(`address program must be ${1 + HASH_SIZE} bytes`);
  }
  if (program[0] === HYBRID_ADDRESS_VERSION) {
    throw new Bech32Error(
      "hybrid address version 1 is not supported in the browser; use the CLI wallet",
    );
  }
  if (program[0] !== ADDRESS_VERSION) {
    throw new Bech32Error(`unsupported address version ${program[0] as number}`);
  }
  const words = convertBits(program, 8, 5, true);
  if (words === null) throw new Bech32Error("cannot convert address program to 5-bit words");
  const address = bech32Encode(hrp, words);
  if (address.length > 90) throw new Bech32Error("address exceeds the 90-character limit");
  return address;
}

/** The human-readable prefix each network uses (`NetworkParams.address_hrp`). */
export const ADDRESS_HRP: Readonly<Record<string, string>> = {
  mainnet: "sccp",
  testnet: "sccpt",
  devnet: "sccpt",
};

/** The HRP for a network id, defaulting to the testnet prefix. */
export function addressHrp(network: string): string {
  return ADDRESS_HRP[network] ?? "sccpt";
}
