/**
 * Read a file from this computer: the other direction of `download.ts`.
 *
 * A keyfile is a document a person moves between `sccp wallet` and the browser,
 * and the export side already produces a real file. Importing one should not be a
 * copy-paste exercise, so this opens the platform's file picker and reads the
 * chosen file as text. Nothing is uploaded: the picker hands the page a `File`
 * handle, `File.text()` decodes it locally, and the caller decides what to do
 * with the string.
 *
 * The size limit is not a security control, it is a courtesy: a keyfile is a few
 * kilobytes (a 3970-hex public key and a 96-hex ciphertext), so a multi-megabyte
 * selection is the wrong file, and saying that beats decrypting it.
 */

import { formatInteger } from "./format";

/** What a keyfile weighs, rounded up generously for a pretty-printed one. */
export const KEYFILE_MAX_BYTES = 64 * 1024;

/**
 * What the picker offers. The CLI writes `sccp-<address>.key`; a file saved from
 * the browser's download or copied out of a chat arrives as `.json` or `.txt`.
 */
export const KEYFILE_ACCEPT = ".key,.json,.txt,application/json,text/plain,application/octet-stream";

export class FileError extends Error {
  override name = "FileError";
}

/** The parts of `File` a reader needs, so a test can stand in for a picker. */
export interface PickedFile {
  readonly name: string;
  readonly size: number;
  text(): Promise<string>;
}

/**
 * Read a picked file as text, refusing what cannot be a keyfile.
 *
 * The two refusals are the ones worth naming: an empty file (a save that went
 * wrong) and one far larger than a keyfile (the wrong file). Everything else is
 * left to `parseKeyfile`, which explains a malformed document field by field.
 */
export async function readTextFile(
  file: PickedFile,
  options: { maxBytes?: number } = {},
): Promise<string> {
  const limit = options.maxBytes ?? KEYFILE_MAX_BYTES;
  if (file.size > limit) {
    throw new FileError(
      `that file is ${formatInteger(file.size)} bytes; a keyfile is under ${formatInteger(limit)}`,
    );
  }
  const text = await file.text();
  if (text.trim().length === 0) throw new FileError(`${file.name} is empty`);
  return text;
}

/**
 * Ask for a file and resolve it, or `null` when the dialog was dismissed.
 *
 * An `<input type="file">` is the only way a page may open the picker, and its
 * click has to happen inside the user gesture that called this — so the element
 * is created, clicked and removed here rather than rendered by the view. It is
 * positioned off-screen instead of `display: none`, because some browsers ignore
 * a click on a control that is not laid out.
 *
 * Cancelling fires `cancel` (Chrome 113+, Safari 16.4+); where it does not, the
 * window getting focus back with no selection is the same event, so both paths
 * end at `finish(null)` and only the first one counts.
 */
export function pickTextFile(accept: string = KEYFILE_ACCEPT): Promise<PickedFile | null> {
  if (typeof globalThis.document === "undefined") {
    return Promise.reject(new FileError("this environment cannot open a file picker"));
  }
  return new Promise<PickedFile | null>((resolve) => {
    const input = globalThis.document.createElement("input");
    input.type = "file";
    input.accept = accept;
    input.style.position = "fixed";
    input.style.top = "0";
    input.style.left = "-1000px";
    input.style.width = "1px";
    input.style.height = "1px";
    input.style.opacity = "0";

    let settled = false;
    const finish = (file: PickedFile | null): void => {
      if (settled) return;
      settled = true;
      input.remove();
      globalThis.removeEventListener("focus", onFocus);
      resolve(file);
    };
    // `focus` comes back when the dialog closes, with or without a selection. The
    // tick lets a `change` that is already queued be handled first.
    const onFocus = (): void => {
      setTimeout(() => finish(input.files?.[0] ?? null), 300);
    };

    input.addEventListener("change", () => finish(input.files?.[0] ?? null));
    input.addEventListener("cancel", () => finish(null));
    globalThis.addEventListener("focus", onFocus);
    globalThis.document.body.append(input);
    input.click();
  });
}
