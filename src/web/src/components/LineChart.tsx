/**
 * A dependency-free SVG line chart.
 *
 * `react-native-web` renders to the DOM, so this is a real `<svg>`: the axes,
 * the area fill and the per-point `<title>` tooltips come from the browser with
 * no charting package. Width comes from `onLayout`, which keeps the drawing
 * coordinates in real pixels so the labels never stretch with the container.
 */

import { useMemo, useState, type ReactNode } from "react";
import { StyleSheet, View, type LayoutChangeEvent } from "react-native";
import { Text } from "react-native-paper";

import { chartGeometry, type ChartDatum } from "../lib/chart";

export function LineChart({
  points,
  height = 240,
  colour,
  textColour,
  label,
  emptyLabel = "No data yet",
}: {
  points: readonly ChartDatum[];
  height?: number;
  /** Stroke and fill colour, usually the theme's primary. */
  colour: string;
  /** Axis label colour, usually the theme's onSurface. */
  textColour: string;
  /** Accessible name for the image. */
  label: string;
  emptyLabel?: string;
}): ReactNode {
  const [width, setWidth] = useState(0);
  const geometry = useMemo(
    () => chartGeometry(points, width, height),
    [points, width, height],
  );
  const first = points[0];
  const last = points[points.length - 1];
  const grid = "rgba(128,128,128,0.28)";

  const onLayout = (event: LayoutChangeEvent): void => {
    setWidth(event.nativeEvent.layout.width);
  };

  return (
    <View style={[styles.frame, { height }]} onLayout={onLayout}>
      {width > 0 && first !== undefined && last !== undefined ? (
        <svg width={width} height={height} role="img" aria-label={label}>
          {geometry.ticks.map((tick) => (
            <g key={tick.value}>
              <line
                x1={geometry.plot.left}
                x2={geometry.plot.right}
                y1={tick.y}
                y2={tick.y}
                stroke={grid}
                strokeWidth={1}
              />
              <text
                x={geometry.plot.left - 8}
                y={tick.y + 4}
                textAnchor="end"
                fontSize={11}
                fill={textColour}
                opacity={0.66}
              >
                {Math.round(tick.value)}
              </text>
            </g>
          ))}

          {geometry.dots.length > 1 && (
            <path d={geometry.area} fill={colour} opacity={0.12} />
          )}
          {geometry.dots.length > 1 && (
            <path
              d={geometry.path}
              fill="none"
              stroke={colour}
              strokeWidth={2}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          )}

          {geometry.dots.map((dot, index) => (
            <circle
              key={`${dot.datum.x}-${index}`}
              cx={dot.x}
              cy={dot.y}
              r={index === geometry.dots.length - 1 ? 4 : 2.5}
              fill={colour}
            >
              {dot.datum.label !== undefined && <title>{dot.datum.label}</title>}
            </circle>
          ))}

          <text
            x={geometry.plot.left}
            y={height - 6}
            fontSize={11}
            fill={textColour}
            opacity={0.66}
          >
            {`epoch ${first.x}`}
          </text>
          {last.x !== first.x && (
            <text
              x={geometry.plot.right}
              y={height - 6}
              textAnchor="end"
              fontSize={11}
              fill={textColour}
              opacity={0.66}
            >
              {`epoch ${last.x}`}
            </text>
          )}
        </svg>
      ) : (
        <View style={styles.empty}>
          <Text variant="bodySmall" style={styles.muted}>
            {emptyLabel}
          </Text>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  frame: { width: "100%", justifyContent: "center" },
  empty: { alignItems: "center" },
  muted: { opacity: 0.66 },
});
