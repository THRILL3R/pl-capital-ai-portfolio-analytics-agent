/** RFC 4180 CSV with a guard against spreadsheet formula injection from text cells. */
function escapeCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  let text = String(value);
  if (typeof value === 'string' && /^[=+@\t\r]/.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function toCsv(columns: string[], rows: Record<string, unknown>[]): string {
  return [columns.map(escapeCell).join(','), ...rows.map((row) => columns.map((c) => escapeCell(row[c])).join(','))].join('\r\n');
}
