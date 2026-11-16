/**
 * The browser entry point.
 *
 * `App` owns the light/dark switch, so it also owns the `PaperProvider` that
 * feeds that choice to every Material component; this file only mounts it.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { currentSite } from "./lib/site";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("index.html has no #root element to mount the explorer on");
}

// One bundle, two names: the tab has to say which one it is. The static title in
// index.html is the explorer's, which is also what a crawler and a `curl` see.

const site = currentSite();
const titles: Record<ReturnType<typeof currentSite>, string> = {
  explorer: "SCCP explorer",
  wallet: "SCCP wallet",
  play: "SCCP play",
  dex: "SCCP DEX",
  bridge: "SCCP bridge",
};
document.title = titles[site];

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
