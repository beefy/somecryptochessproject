/**
 * The file reader behind "Open a keyfile": size, emptiness and the picker.
 *
 * `readTextFile` is the part with rules in it and takes anything shaped like a
 * `File`. `pickTextFile` needs a DOM, so it is driven here through a stub
 * `<input>`: what matters is that a chosen file resolves, a dismissed dialog
 * resolves `null` (and therefore cannot leave the button spinning), and the
 * element and its listener are gone either way.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { FileError, KEYFILE_MAX_BYTES, pickTextFile, readTextFile, type PickedFile } from "./upload";

function picked(text: string, name = "sccp-sccpt1qtest.key"): PickedFile {
  return { name, size: new TextEncoder().encode(text).length, text: async () => text };
}

describe("readTextFile", () => {
  it("returns the file's text untouched", async () => {
    const contents = '{"version":2}\n';
    await expect(readTextFile(picked(contents))).resolves.toBe(contents);
  });

  it("refuses an empty file by name", async () => {
    await expect(readTextFile(picked("   \n"))).rejects.toThrow(FileError);
    await expect(readTextFile(picked(""))).rejects.toThrow(/sccp-sccpt1qtest\.key is empty/);
  });

  it("refuses a file far larger than a keyfile, and says both sizes", async () => {
    const huge: PickedFile = { name: "photo.png", size: 4_000_000, text: async () => "" };
    await expect(readTextFile(huge)).rejects.toThrow(/4,000,000 bytes; a keyfile is under 65,536/);
  });

  it("takes the limit from the caller, so a test need not write 64 KiB", async () => {
    const small: PickedFile = { name: "a.key", size: 9, text: async () => "{}" };
    await expect(readTextFile(small, { maxBytes: 8 })).rejects.toThrow(/9 bytes/);
    await expect(readTextFile(small, { maxBytes: KEYFILE_MAX_BYTES })).resolves.toBe("{}");
  });

  it("passes a read failure through instead of pretending the file was empty", async () => {
    const broken: PickedFile = {
      name: "a.key",
      size: 10,
      text: async () => {
        throw new DOMException("the file changed on disk", "NotReadableError");
      },
    };
    await expect(readTextFile(broken)).rejects.toThrow(/NotReadableError|file changed/);
  });
});

/** Just enough of `<input type="file">` for `pickTextFile`, with events on tap. */
class StubInput {
  type = "";
  accept = "";
  readonly style: Record<string, string> = {};
  files: PickedFile[] | null = null;
  clicked = 0;
  removed = 0;
  private readonly handlers = new Map<string, () => void>();

  addEventListener(type: string, handler: () => void): void {
    this.handlers.set(type, handler);
  }

  remove(): void {
    this.removed += 1;
  }

  click(): void {
    this.clicked += 1;
  }

  /** Fire one of `change` or `cancel`, as the browser would. */
  emit(type: string): void {
    this.handlers.get(type)?.();
  }
}

describe("pickTextFile", () => {
  const document = { createElement: vi.fn(), body: { append: vi.fn() } };
  const listeners = new Map<string, () => void>();

  function stubDom(): StubInput {
    const input = new StubInput();
    document.createElement.mockReturnValue(input);
    vi.stubGlobal("document", document);
    vi.stubGlobal("addEventListener", (type: string, handler: () => void) => {
      listeners.set(type, handler);
    });
    vi.stubGlobal("removeEventListener", (type: string) => {
      listeners.delete(type);
    });
    return input;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    listeners.clear();
  });

  it("opens the picker on the gesture and resolves the chosen file", async () => {
    const input = stubDom();
    const pending = pickTextFile();
    expect(input.clicked).toBe(1);
    expect(input.type).toBe("file");
    expect(input.accept).toContain(".key");
    // The picker must not default to every file on disk.
    expect(document.body.append).toHaveBeenCalledWith(input);

    const file = picked('{"version":2}');
    input.files = [file];
    input.emit("change");
    await expect(pending).resolves.toBe(file);
    expect(input.removed).toBe(1);
    expect(listeners.has("focus")).toBe(false);
  });

  it("resolves null when the dialog is dismissed, and only once", async () => {
    const input = stubDom();
    const pending = pickTextFile();
    input.emit("cancel");
    input.emit("change");
    await expect(pending).resolves.toBeNull();
    expect(input.removed).toBe(1);
  });

  it("treats a change with no file as a dismissal rather than an empty pick", async () => {
    const input = stubDom();
    const pending = pickTextFile();
    input.files = null;
    input.emit("change");
    await expect(pending).resolves.toBeNull();
  });

  it("falls back to the window regaining focus where there is no cancel event", async () => {
    vi.useFakeTimers();
    try {
      const input = stubDom();
      const pending = pickTextFile();
      listeners.get("focus")?.();
      vi.advanceTimersByTime(300);
      await expect(pending).resolves.toBeNull();
      expect(input.removed).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });
});
