/**
 * Match certificates: the start-of-match commitment both seats sign.
 *
 * Ported from `sccp/protocol/transcript.py::certificate_payload`. The relay
 * builds the certificate (it owns the opening and the engine fingerprints);
 * this module only rebuilds the signed payload so a seat can prove it agreed to
 * *that* match, with *that* colour, book line and clock.
 */

import type { MatchCertificate, OpeningRef, TimeControl } from "../types";
import { DOMAINS, domainHex } from "./canonical";
import { type Keypair, signDomain } from "./keys";
import { sortedSeats } from "./protocol";

/** The exact payload a seat signs to join a match (PROTOCOL.md section 8.1). */
export function certificatePayload(certificate: MatchCertificate): Record<string, unknown> {
  return {
    network: certificate.network,
    match_id: certificate.match_id,
    epoch: certificate.epoch,
    round: certificate.round,
    seats: sortedSeats([certificate.white, certificate.black]),
    colors: { white: certificate.white, black: certificate.black },
    opening_hash: certificate.opening.hash,
    time_control: controlFields(certificate.time_control),
    engine_fingerprints: {
      white: certificate.engine_white,
      black: certificate.engine_black,
    },
  };
}

function controlFields(control: TimeControl): Record<string, number> {
  return { initial_ms: control.initial_ms, increment_ms: control.increment_ms };
}

/** `H("sccp/v1/opening", {book_version, line_id, moves})` as hex. */
export function openingHash(opening: OpeningRef): string {
  return domainHex(DOMAINS.OPENING, {
    book_version: opening.book_version,
    line_id: opening.line_id,
    moves: [...opening.moves],
  });
}

/** Sign a certificate as one seat; hex signature, ready for `POST .../join`. */
export function signCertificate(keypair: Keypair, certificate: MatchCertificate): string {
  return signDomain(keypair, DOMAINS.CERTIFICATE, certificatePayload(certificate));
}
