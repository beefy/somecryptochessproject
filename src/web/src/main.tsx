/**
 * The browser entry point.
 *
 * `App` owns the light/dark switch, so it also owns the `PaperProvider` that
 * feeds that choice to every Material component; this file only mounts it.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("index.html has no #root element to mount the explorer on");
}

// index.html owns the initial title (the explorer's) for a crawler and a `curl`.
// App updates it once the hash route names the view.
createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
