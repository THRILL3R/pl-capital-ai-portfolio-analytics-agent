import type { ExposureResult, Holding } from '../api/types';

/** Categorical series: indigo, blue, lavender, violet, slate. Never green (green = gain/success). */
export const CATEGORICAL = ['#0000A0', '#4C4CCC', '#3922D1', '#7D7DD0', '#272756', '#B6B6F7', '#6B7280', '#D0D0FC'] as const;

export const colorFor = (index: number): string => CATEGORICAL[index % CATEGORICAL.length];

/**
 * Weight that is not part of the equity exposure = 100 - total equity weight.
 * DERIVED value: the backend does not return a bond weight, so this is labelled
 * "Excluded from exposure" and never "bonds".
 */
export function excludedFromExposurePct(totalEquityWeightPct: number): number {
  const remainder = Math.round((100 - totalEquityWeightPct) * 10000) / 10000;
  return remainder > 0 ? remainder : 0;
}

export interface DonutSegment {
  label: string;
  pct: number;
  color: string;
  excluded?: boolean;
}

export function donutSegments(result: ExposureResult): DonutSegment[] {
  const segments: DonutSegment[] = result.exposures.map((e, i) => ({
    label: e.sector,
    pct: e.exposure_pct,
    color: colorFor(i),
  }));
  const excluded = excludedFromExposurePct(result.total_equity_weight_pct);
  if (excluded > 0) segments.push({ label: 'Excluded from exposure', pct: excluded, color: '#E2E8F0', excluded: true });
  return segments;
}

/** Largest Stock holding (by current_weight) per sector, from the real holdings rows. */
export function largestStockBySector(holdings: Holding[]): Record<string, Holding> {
  const best: Record<string, Holding> = {};
  for (const h of holdings) {
    if (h.asset_type !== 'Stock' || !h.sector) continue;
    if (!best[h.sector] || h.current_weight > best[h.sector].current_weight) best[h.sector] = h;
  }
  return best;
}
