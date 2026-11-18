/**
 * The light/dark switch: a sun in dark mode, a moon in light mode.
 *
 * The glyph is drawn here rather than through `IconButton icon="weather-night"`:
 * react-native-paper renders icons through an icon library and this bundle ships
 * none, so an icon name is an empty circle. `react-native-web` renders to the
 * DOM, so a real `<svg>` works — `LineChart` already relies on that.
 *
 * The icon shows what a press switches *to*: in dark mode the button offers the
 * light scheme, so it shows a sun.
 */

import type { ReactNode } from "react";
import { Pressable, StyleSheet, View } from "react-native";
import { useTheme } from "react-native-paper";

import { useThemeMode } from "../theme";

const SIZE = 20;

/** A sun: a disc and eight rays. */
function SunIcon({ colour }: { colour: string }): ReactNode {
  const rays = Array.from({ length: 8 }, (_, index) => (index * Math.PI) / 4);
  return (
    <svg width={SIZE} height={SIZE} viewBox="0 0 24 24" role="presentation" focusable="false">
      <circle cx={12} cy={12} r={4.4} fill={colour} />
      {rays.map((angle) => {
        const inner = 7.2;
        const outer = 10.4;
        return (
          <line
            key={angle}
            x1={12 + Math.cos(angle) * inner}
            y1={12 + Math.sin(angle) * inner}
            x2={12 + Math.cos(angle) * outer}
            y2={12 + Math.sin(angle) * outer}
            stroke={colour}
            strokeWidth={1.9}
            strokeLinecap="round"
          />
        );
      })}
    </svg>
  );
}

/** A crescent: one disc with a second, offset disc cut out of it. */
function MoonIcon({ colour }: { colour: string }): ReactNode {
  return (
    <svg width={SIZE} height={SIZE} viewBox="0 0 24 24" role="presentation" focusable="false">
      <path
        d="M20.5 14.3A8.6 8.6 0 0 1 9.7 3.5 8.6 8.6 0 1 0 20.5 14.3Z"
        fill={colour}
      />
    </svg>
  );
}

export function ThemeToggle(): ReactNode {
  const { mode, toggle } = useThemeMode();
  const { colors } = useTheme();
  const dark = mode === "dark";
  return (
    <Pressable
      onPress={toggle}
      accessibilityRole="button"
      accessibilityLabel={dark ? "Switch to the light colour scheme" : "Switch to the dark colour scheme"}
      style={styles.button}
      hitSlop={8}
    >
      <View style={styles.icon}>
        {dark ? <SunIcon colour={colors.onSurface} /> : <MoonIcon colour={colors.onSurface} />}
      </View>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  button: {
    width: 40,
    height: 40,
    borderRadius: 20,
    justifyContent: "center",
    alignItems: "center",
  },
  icon: { width: SIZE, height: SIZE, alignItems: "center", justifyContent: "center" },
});
