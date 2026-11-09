import { useMemo } from 'react';
import { formatSql, highlightSql, type TokenKind } from '../lib/sql';

/** Colours meet WCAG AA on white. Green is deliberately not used (reserved for gains/success). */
const KIND_CLASS: Record<TokenKind, string> = {
  keyword: 'font-bold text-brand-blue',
  function: 'text-violet-accent',
  table: 'font-semibold text-indigo-900',
  alias: 'text-secondary',
  column: 'text-indigo-700',
  string: 'text-[#9D174D]',
  number: 'text-[#A16207]',
  param: 'rounded bg-lavender-input px-0.5 font-bold text-indigo-800',
  operator: 'text-text-muted',
  punct: 'text-text-muted',
  text: '',
};

const HANG = 4; // wrapped continuation lines sit this many characters deeper than their line

/**
 * Display-only rendering of generated SQL: 4-space indentation, one column / JOIN / clause per line,
 * and syntax colours. The `sql` prop is never changed; copy buttons must use the original string.
 */
export function SqlCode({ sql, params = [] }: { sql: string; params?: unknown[] }) {
  const lines = useMemo(() => highlightSql(formatSql(sql)), [sql]);
  return (
    <div role="region" aria-label="Generated SQL, formatted" tabIndex={0} className="mt-2 rounded-md bg-surface-base p-3 font-mono text-[12px] leading-relaxed text-indigo-700">
      <code className="block">
        {lines.map((line, i) => (
          <span
            key={i}
            className="block"
            // Hanging indent: the first row starts at the SQL indent; wrapped rows start HANG chars deeper.
            style={{ paddingLeft: `${line.indent + HANG}ch`, textIndent: `-${HANG}ch`, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}
          >
            {line.tokens.map((t, j) => (
              <span
                key={j}
                className={KIND_CLASS[t.kind]}
                title={t.kind === 'param' && t.param !== undefined && t.param < params.length ? `Bound parameter ${t.param + 1}: ${JSON.stringify(params[t.param])}` : undefined}
              >
                {t.text}
              </span>
            ))}
          </span>
        ))}
      </code>
    </div>
  );
}
