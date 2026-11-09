import { formatCell, humanizeColumn, isNumericColumn } from '../lib/format';

interface Props {
  columns: string[];
  rows: Record<string, unknown>[];
  caption: string;
}

/** Generic result table: columns and rows come straight from the backend result. */
export function DataTable({ columns, rows, caption }: Props) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border-hairline" role="region" aria-label={caption} tabIndex={0}>
      <table className="w-full text-left text-table-cell">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="table-head">
            {columns.map((c) => (
              <th key={c} scope="col" className={`px-4 py-3 ${isNumericColumn(rows, c) ? 'text-right' : ''}`}>
                {humanizeColumn(c)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border-table-row">
          {rows.map((row, i) => (
            <tr key={i} className="transition-colors hover:bg-surface">
              {columns.map((c, j) => {
                const numeric = isNumericColumn(rows, c);
                return (
                  <td key={c} className={`px-4 py-3 ${numeric ? 'tabular text-right' : ''} ${j === 0 && !numeric ? 'font-semibold text-indigo-800' : 'text-text-primary'}`}>
                    {formatCell(c, row[c])}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
