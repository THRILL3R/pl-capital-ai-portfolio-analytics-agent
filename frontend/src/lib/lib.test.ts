import { describe, expect, it } from 'vitest';
import type { ExposureResult, Holding } from '../api/types';
import { toCsv } from './csv';
import { CATEGORICAL, donutSegments, excludedFromExposurePct, largestStockBySector } from './exposure';
import { classifyColumn, formatCell, formatCurrency, formatNumber, formatPercent, hasCurrencyColumn, humanizeColumn } from './format';

describe('formatting', () => {
  it('formats USD with thousands separators and no decimals for whole amounts', () => {
    expect(formatCurrency(438000)).toBe('$438,000');
    expect(formatCurrency(85000000)).toBe('$85,000,000');
    expect(formatCurrency(1565292.5)).toBe('$1,565,292.50');
    expect(formatCurrency(-4140)).toBe('-$4,140');
  });

  it('formats percentages without padding and never as a currency', () => {
    expect(formatPercent(36)).toBe('36%');
    expect(formatPercent(84.67)).toBe('84.67%');
    expect(formatPercent(22.123456)).toBe('22.12%');
  });

  it('classifies result columns conservatively by name', () => {
    expect(classifyColumn('total_aum')).toBe('currency');
    expect(classifyColumn('avg_price')).toBe('currency');
    expect(classifyColumn('cost_basis')).toBe('currency');
    expect(classifyColumn('tech_value')).toBe('currency');
    expect(classifyColumn('tech_percentage')).toBe('percent');
    expect(classifyColumn('exposure_pct')).toBe('percent');
    // Unknown names stay plain numbers: never claim a unit that is not known.
    expect(classifyColumn('quantity')).toBe('number');
    expect(classifyColumn('diversification_ratio')).toBe('number');
    expect(classifyColumn('holding_id')).toBe('number');
    expect(classifyColumn('COUNT(*)')).toBe('number');
  });

  it('formats cells, including nulls and plain numbers', () => {
    expect(formatCell('cost_basis', 438000)).toBe('$438,000');
    expect(formatCell('quantity', 2500)).toBe('2,500');
    expect(formatCell('tech_percentage', 84.67)).toBe('84.67%');
    expect(formatCell('anything', null)).toBe('—');
    expect(formatCell('symbol', 'AAPL')).toBe('AAPL');
    expect(formatNumber(0.5)).toBe('0.5');
  });

  it('detects currency columns only among numeric columns', () => {
    const rows = [{ name: 'x', total_aum: 5 }];
    expect(hasCurrencyColumn(['name', 'total_aum'], rows)).toBe(true);
    expect(hasCurrencyColumn(['name'], rows)).toBe(false);
    expect(humanizeColumn('avg_price')).toBe('Avg price');
  });
});

describe('csv export', () => {
  it('quotes commas, quotes and newlines', () => {
    expect(toCsv(['a', 'b'], [{ a: 'x,y', b: 'say "hi"' }])).toBe('a,b\r\n"x,y","say ""hi"""');
  });
  it('writes numbers raw and nulls empty', () => {
    expect(toCsv(['n', 'z'], [{ n: 438000, z: null }])).toBe('n,z\r\n438000,');
  });
  it('neutralises spreadsheet formula injection in text cells', () => {
    expect(toCsv(['c'], [{ c: '=HYPERLINK("http://x")' }])).toContain("'=HYPERLINK");
    expect(toCsv(['c'], [{ c: -4.2 }])).toBe('c\r\n-4.2');
  });
});

const exposure = (total: number, exposures: [string, number][]): ExposureResult => ({
  status: 'success', tool: 'exposure_calculator', portfolio_id: 4, portfolio_name: 'Balanced Portfolio',
  exposure_basis: '', asset_filter: '', total_holdings_count: 8, equity_holdings_count: 4, excluded_bond_holdings: 4,
  unmapped_equity_holdings: 0, error: null, total_equity_weight_pct: total,
  exposures: exposures.map(([sector, pct]) => ({ sector, exposure_pct: pct, weight_fraction: pct / 100, holding_count: 1 })),
});

describe('exposure helpers', () => {
  it('derives the unfilled remainder as 100 minus equity weight, never negative', () => {
    expect(excludedFromExposurePct(36)).toBe(64);
    expect(excludedFromExposurePct(100)).toBe(0);
    expect(excludedFromExposurePct(100.5)).toBe(0);
  });

  it('labels the remainder "Excluded from exposure" (never "bonds") and omits it at 100%', () => {
    const mixed = donutSegments(exposure(36, [['Technology', 22], ['Financials', 8], ['Healthcare', 6]]));
    const rest = mixed[mixed.length - 1];
    expect(rest.label).toBe('Excluded from exposure');
    expect(rest.pct).toBe(64);
    expect(rest.excluded).toBe(true);
    expect(mixed.map((s) => s.label).join(' ')).not.toMatch(/bond/i);
    expect(donutSegments(exposure(100, [['Energy', 38], ['Technology', 36], ['Automotive', 15], ['Consumer Staples', 11]]))).toHaveLength(4);
  });

  it('never assigns a green or red colour to a sector', () => {
    const forbidden = ['#22C55E', '#1FB15A', '#E53935'];
    for (const c of CATEGORICAL) expect(forbidden).not.toContain(c.toUpperCase());
    const segs = donutSegments(exposure(100, Array.from({ length: 10 }, (_, i) => [`S${i}`, 10] as [string, number])));
    for (const s of segs) expect(forbidden).not.toContain(s.color.toUpperCase());
  });

  it('picks the largest Stock holding per sector and ignores bonds', () => {
    const h = (symbol: string, sector: string | null, weight: number, type = 'Stock'): Holding => ({ symbol, company_name: symbol, asset_type: type, sector, quantity: 1, cost_basis: 1, current_price: 1, current_weight: weight });
    const best = largestStockBySector([h('AAPL', 'Technology', 0.12), h('MSFT', 'Technology', 0.1), h('UST', null, 0.25, 'Bond'), h('JPM', 'Financials', 0.08)]);
    expect(best.Technology.symbol).toBe('AAPL');
    expect(best.Financials.symbol).toBe('JPM');
    expect(Object.keys(best)).toHaveLength(2);
  });
});
