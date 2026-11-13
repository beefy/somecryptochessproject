/**
 * The MD3 theme pair and the few tokens MD3 does not have.
 *
 * `mono` is the one the explorer needs: hashes, keys and signatures must line up
 * in a column, and a proportional font makes them unreadable.
 */

import { createContext, useContext } from "react";
import { Platform } from "react-native";
import { MD3DarkTheme, MD3LightTheme, type MD3Theme } from "react-native-paper";

export type ThemeMode = "light" | "dark";

/** Font stack for hex, PGN and JSON. */
export const monospace = Platform.select({
  ios: "Menlo",
  android: "monospace",
  default: "ui-monospace, SFMono-Regular, Menlo, monospace",
});

export type AppTheme = MD3Theme & { fonts: MD3Theme["fonts"] & { mono: MD3Theme["fonts"]["bodyMedium"] } };

function withMono(base: MD3Theme): AppTheme {
  return { ...base, fonts: { ...base.fonts, mono: { ...base.fonts.bodyMedium, fontFamily: monospace } } };
}

export const lightTheme: AppTheme = withMono(MD3LightTheme);
export const darkTheme: AppTheme = withMono(MD3DarkTheme);

/** The browser's preference, used as the initial mode. */
export function preferredMode(): ThemeMode {
  if (typeof window === "undefined") return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export const ThemeModeContext = createContext<{ mode: ThemeMode; toggle: () => void }>({
  mode: "light",
  toggle: () => undefined,
});

/** The current colour mode and a way to flip it. */
export function useThemeMode(): { mode: ThemeMode; toggle: () => void } {
  return useContext(ThemeModeContext);
}
