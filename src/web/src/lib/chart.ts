/**
 * Chart geometry, kept out of the component so it can be tested without a DOM.
 *
 * The explorer draws one small SVG line chart (a miner's rating per epoch) and
 * has no charting dependency: the mapping from data to pixels is the only part
 * that can be wrong, so it lives here and the component stays presentational.
 */

/** One plottable value. `label` is shown as the point's SVG tooltip. */
export interface ChartDatum {
  x: number;
  y: number;
  label?: string;
}

export interface ChartPadding {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

/** Room for the y-axis labels on the left and the epoch labels below. */
export const CHART_PADDING: ChartPadding = { top: 14, right: 16, bottom: 26, left: 48 };

export interface ChartDot {
  x: number;
  y: number;
  datum: ChartDatum;
}

export interface ChartTick {
  y: number;
  value: number;
}

export interface ChartGeometry {
  /** An SVG path through the points, in order. */
  path: string;
  /** The same path closed to the baseline, for a faint area fill. */
  area: string;
  dots: ChartDot[];
  /** Three grid lines: minimum, middle and maximum of the padded domain. */
  ticks: ChartTick[];
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
  plot: { left: number; right: number; top: number; bottom: number };
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}

/**
 * Map `points` into `width`x`height`, with a little vertical breathing room.
 *
 * A flat series is centred rather than pinned to an edge, so a miner who drew
 * every game still gets a readable line. A single point is centred
 * horizontally: there is no interval to spread it over.
 */
export function chartGeometry(
  points: readonly ChartDatum[],
  width: number,
  height: number,
  padding: ChartPadding = CHART_PADDING,
): ChartGeometry {
  const plot = {
    left: padding.left,
    right: Math.max(padding.left, width - padding.right),
    top: padding.top,
    bottom: Math.max(padding.top, height - padding.bottom),
  };
  const empty: ChartGeometry = {
    path: "",
    area: "",
    dots: [],
    ticks: [],
    minX: 0,
    maxX: 0,
    minY: 0,
    maxY: 0,
    plot,
  };
  if (points.length === 0 || width <= 0 || height <= 0) return empty;

  let minX = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const point of points) {
    if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) return empty;
    minX = Math.min(minX, point.x);
    maxX = Math.max(maxX, point.x);
    minY = Math.min(minY, point.y);
    maxY = Math.max(maxY, point.y);
  }

  const span = maxY - minY;
  const margin = span === 0 ? Math.max(1, Math.abs(maxY) * 0.01) : span * 0.08;
  minY -= margin;
  maxY += margin;

  const xSpan = maxX - minX;
  const scaleX = (value: number): number =>
    xSpan === 0
      ? (plot.left + plot.right) / 2
      : plot.left + ((value - minX) / xSpan) * (plot.right - plot.left);
  const scaleY = (value: number): number =>
    plot.bottom - ((value - minY) / (maxY - minY)) * (plot.bottom - plot.top);

  const dots: ChartDot[] = points.map((datum) => ({
    x: round(scaleX(datum.x)),
    y: round(scaleY(datum.y)),
    datum,
  }));
  const first = dots[0];
  const last = dots[dots.length - 1];
  if (first === undefined || last === undefined) return empty;

  const path = dots
    .map((dot, index) => `${index === 0 ? "M" : "L"}${dot.x},${dot.y}`)
    .join(" ");
  const area = `${path} L${last.x},${round(plot.bottom)} L${first.x},${round(plot.bottom)} Z`;
  const ticks = [minY, (minY + maxY) / 2, maxY].map((value) => ({
    y: round(scaleY(value)),
    value,
  }));
  return { path, area, dots, ticks, minX, maxX, minY, maxY, plot };
}
