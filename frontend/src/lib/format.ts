/** Number and cell formatting. Pure functions; no data is invented here. */

const NUMBER_FORMAT = (maxFraction: number, minFraction = 0) =>
  new Intl.NumberFormat('en-US', { minimumFractionDigits: minFraction, maximumFractionDigits: maxFraction });

export function formatNumber(value: number, maxFraction = 4): string {
  return NUMBER_FORMAT(maxFraction).format(value);
}

/** USD: the sample dataset is US-listed and priced in USD. Whole amounts show no decimals. */
export function formatCurrency(value: number): string {
  const sign = value < 0 ? '-' : '';
  const fraction = Number.isInteger(value) ? 0 : 2;
  return `${sign}$${NUMBER_FORMAT(2, fraction).format(Math.abs(value))}`;
}

export function formatPercent(value: number, maxFraction = 2): string {
  return `${formatNumber(value, maxFraction)}%`;
}

export type ColumnKind = 'currency' | 'percent' | 'number';

/**
 * Best-effort classification from a result column NAME (Gemini aliases vary). Unknown names
 * fall back to a plain number, which never misstates a unit.
 */
export function classifyColumn(name: string): ColumnKind {
  const n = name.toLowerCase();
  if (/(pct|percent|percentage)/.test(n)) return 'percent';
  if (/(^|_)(aum|value|price|cost|basis|amount|market)(_|$)/.test(n) || /(aum|_val$)/.test(n)) return 'currency';
  return 'number';
}

export function formatCell(column: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    switch (classifyColumn(column)) {
      case 'currency':
        return formatCurrency(value);
      case 'percent':
        return formatPercent(value);
      default:
        return formatNumber(value);
    }
  }
  return String(value);
}

export function isNumericColumn(rows: Record<string, unknown>[], column: string): boolean {
  const values = rows.map((r) => r[column]).filter((v) => v !== null && v !== undefined);
  return values.length > 0 && values.every((v) => typeof v === 'number');
}

export function hasCurrencyColumn(columns: string[], rows: Record<string, unknown>[]): boolean {
  return columns.some((c) => isNumericColumn(rows, c) && classifyColumn(c) === 'currency');
}

/** Column header text from a SQL alias: "avg_price" -> "Avg price". */
export function humanizeColumn(column: string): string {
  const spaced = column.replace(/[_]+/g, ' ').trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
