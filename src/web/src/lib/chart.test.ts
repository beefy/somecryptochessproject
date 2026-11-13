import { describe, expect, it } from "vitest";

import { CHART_PADDING, chartGeometry } from "./chart";

const SIZE = { width: 220, height: 120 };
const PADDING = { top: 10, right: 10, bottom: 20, left: 40 };

describe("chartGeometry", () => {
  it("maps a rising series left-to-right and bottom-to-top", () => {
    const geometry = chartGeometry(
      [
        { x: 0, y: 1500 },
        { x: 10, y: 1600 },
      ],
      SIZE.width,
      SIZE.height,
      PADDING,
    );
    expect(geometry.dots).toHaveLength(2);
    expect(geometry.dots[0]?.x).toBe(40);
    expect(geometry.dots[1]?.x).toBe(210);
    expect(geometry.dots[0]?.y).toBeGreaterThan(geometry.dots[1]?.y ?? 0);
    expect(geometry.path.startsWith("M")).toBe(true);
    expect(geometry.path).toContain("L");
    expect(geometry.area.endsWith("Z")).toBe(true);
  });

  it("keeps a flat series in the middle of the plot", () => {
    const geometry = chartGeometry(
      [
        { x: 3, y: 1500 },
        { x: 4, y: 1500 },
      ],
      SIZE.width,
      SIZE.height,
      PADDING,
    );
    const middle = (PADDING.top + (SIZE.height - PADDING.bottom)) / 2;
    expect(geometry.dots[0]?.y).toBeCloseTo(middle, 1);
    expect(geometry.dots[1]?.y).toBeCloseTo(middle, 1);
    expect(geometry.ticks).toHaveLength(3);
  });

  it("centres a single point horizontally", () => {
    const geometry = chartGeometry([{ x: 7, y: 1620 }], SIZE.width, SIZE.height, PADDING);
    expect(geometry.dots).toHaveLength(1);
    expect(geometry.dots[0]?.x).toBe((PADDING.left + SIZE.width - PADDING.right) / 2);
  });

  it("orders ticks from the domain minimum upward", () => {
    const geometry = chartGeometry(
      [
        { x: 0, y: 1400 },
        { x: 1, y: 1800 },
      ],
      SIZE.width,
      SIZE.height,
      PADDING,
    );
    const [low, middle, high] = geometry.ticks;
    expect(low?.value).toBeCloseTo(geometry.minY);
    expect(middle?.value).toBeCloseTo((geometry.minY + geometry.maxY) / 2);
    expect(high?.value).toBeCloseTo(geometry.maxY);
    expect(low?.y).toBeGreaterThan(high?.y ?? 0);
  });

  it("returns empty geometry without points or space", () => {
    expect(chartGeometry([], SIZE.width, SIZE.height, PADDING).path).toBe("");
    expect(chartGeometry([{ x: 0, y: 1500 }], 0, SIZE.height, PADDING).dots).toEqual([]);
    expect(
      chartGeometry([{ x: Number.NaN, y: 1500 }], SIZE.width, SIZE.height, PADDING).path,
    ).toBe("");
  });

  it("uses the default padding when none is given", () => {
    const geometry = chartGeometry(
      [
        { x: 0, y: 1500 },
        { x: 1, y: 1510 },
      ],
      SIZE.width,
      SIZE.height,
    );
    expect(geometry.plot.left).toBe(CHART_PADDING.left);
    expect(geometry.plot.right).toBe(SIZE.width - CHART_PADDING.right);
  });
});
