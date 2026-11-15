/**
 * Save a text file from the page: the keyfile export, and nothing else.
 *
 * A keyfile is a document a person moves between the browser and `sccp wallet`,
 * so exporting has to produce a real file with a stable name. The bundle only
 * ever runs in a browser (React Native for Web), but `document` is the DOM's, not
 * React's, hence the explicit `globalThis`.
 */

export function downloadText(filename: string, text: string, type = "application/json"): void {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const anchor = globalThis.document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  globalThis.document.body.append(anchor);
  anchor.click();
  anchor.remove();
  // Revoking immediately can cancel the download in some browsers; one turn of
  // the event loop is enough for the click to be handled.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** The name an exported keyfile gets: `sccp-<address>.key`, like the CLI's. */
export function keyfileName(address: string): string {
  return `sccp-${address}.key`;
}
