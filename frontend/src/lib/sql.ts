/**
 * DISPLAY-ONLY SQL formatting and highlighting.
 *
 * The SQL the agent generated is never modified: callers keep the original string for copying and
 * execution. Everything here only builds a more readable representation of it.
 */
import { format } from 'sql-formatter';

const INDENT = 4;

/** Whitespace-insensitive form, used to prove formatting changed layout only. */
const squash = (s: string) => s.replace(/\s+/g, '');

const JOIN_ON = /^(\s*)((?:(?:NATURAL|INNER|CROSS|(?:LEFT|RIGHT|FULL)(?:\s+OUTER)?)\s+)?JOIN\b.*?)\s+ON\s+(.*)$/i;

function postProcess(formatted: string): string {
  const lines = formatted.split('\n');
  const out: string[] = [];
  let joinIndent: number | null = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const join = JOIN_ON.exec(line);
    if (join) {
      // `JOIN t ON a = b`  ->  `JOIN t` / `    ON a = b`
      out.push(`${join[1]}${join[2]}`, `${join[1]}${' '.repeat(INDENT)}ON ${join[3]}`);
      joinIndent = join[1].length;
      continue;
    }
    const leading = line.length - line.trimStart().length;
    if (joinIndent !== null && /^\s*(AND|OR)\b/i.test(line) && leading === joinIndent) {
      out.push(`${' '.repeat(INDENT)}${line}`); // further ON conditions stay under their JOIN
      continue;
    }
    joinIndent = null;
    if (/^\s*(LIMIT|OFFSET)\s*$/i.test(line) && i + 1 < lines.length) {
      out.push(`${line.trimEnd()} ${lines[i + 1].trim()}`); // `LIMIT` + `?`  ->  `LIMIT ?`
      i += 1;
      continue;
    }
    out.push(line);
  }
  return out.join('\n');
}

/**
 * Multi-line, 4-space-indented SQL for display. Falls back to the original text if the formatter
 * fails or if its output differs from the input by anything other than whitespace.
 */
export function formatSql(sql: string): string {
  try {
    const formatted = postProcess(
      format(sql, { language: 'sqlite', tabWidth: INDENT, keywordCase: 'preserve', identifierCase: 'preserve', dataTypeCase: 'preserve', functionCase: 'preserve' }).trimEnd(),
    );
    return squash(formatted) === squash(sql) ? formatted : sql.trim();
  } catch {
    return sql.trim();
  }
}

export type TokenKind = 'keyword' | 'function' | 'table' | 'alias' | 'column' | 'string' | 'number' | 'param' | 'operator' | 'punct' | 'text';

export interface SqlToken {
  text: string;
  kind: TokenKind;
  /** 0-based index among `?` placeholders (kind === 'param'). */
  param?: number;
}

export interface SqlLine {
  /** Leading spaces of the formatted line (rendered as a hanging indent). */
  indent: number;
  tokens: SqlToken[];
}

const KEYWORDS = new Set(
  (
    'SELECT FROM WHERE JOIN INNER LEFT RIGHT FULL OUTER CROSS NATURAL ON USING AND OR NOT IN IS NULL AS GROUP BY ORDER ASC DESC LIMIT OFFSET HAVING WITH RECURSIVE ' +
    'CASE WHEN THEN ELSE END DISTINCT UNION ALL INTERSECT EXCEPT LIKE GLOB BETWEEN EXISTS COLLATE ESCAPE TRUE FALSE OVER PARTITION ' +
    'INTEGER INT REAL TEXT NUMERIC FLOAT DOUBLE DECIMAL BLOB DATE DATETIME'
  ).split(' '),
);

const TOKEN_RE =
  /(\s+)|('(?:''|[^'])*')|("(?:""|[^"])*")|(\d+(?:\.\d+)?)|(\?)|([A-Za-z_][A-Za-z0-9_$]*)|(<=|>=|<>|!=|==|\|\||[=<>+\-*/%])|([(),.;])|(.)/g;

interface Raw { text: string; kind: TokenKind | 'ws' | 'ident' | 'quoted'; }

function lex(text: string): Raw[] {
  const tokens: Raw[] = [];
  for (const m of text.matchAll(TOKEN_RE)) {
    if (m[1] !== undefined) tokens.push({ text: m[1], kind: 'ws' });
    else if (m[2] !== undefined) tokens.push({ text: m[2], kind: 'string' });
    else if (m[3] !== undefined) tokens.push({ text: m[3], kind: 'quoted' });
    else if (m[4] !== undefined) tokens.push({ text: m[4], kind: 'number' });
    else if (m[5] !== undefined) tokens.push({ text: m[5], kind: 'param' });
    else if (m[6] !== undefined) tokens.push({ text: m[6], kind: KEYWORDS.has(m[6].toUpperCase()) ? 'keyword' : 'ident' });
    else if (m[7] !== undefined) tokens.push({ text: m[7], kind: 'operator' });
    else if (m[8] !== undefined) tokens.push({ text: m[8], kind: 'punct' });
    else tokens.push({ text: m[9], kind: 'text' });
  }
  return tokens;
}

const isName = (t: Raw | undefined) => t !== undefined && (t.kind === 'ident' || t.kind === 'quoted');
const upper = (t: Raw | undefined) => (t ? t.text.toUpperCase() : '');

/** Assign roles (table / alias / column / function) to identifiers, using clause context. */
function classify(raw: Raw[]): void {
  const sig = raw.filter((t) => t.kind !== 'ws');
  const tables = new Set<string>();
  const aliases = new Set<string>();
  const role = new Map<Raw, TokenKind>();
  const name = (t: Raw) => t.text.replace(/^"|"$/g, '').toLowerCase();

  for (let i = 0; i < sig.length; i++) {
    const t = sig[i];
    // FROM / JOIN <table> [AS] <alias>
    if (t.kind === 'keyword' && (upper(t) === 'FROM' || upper(t) === 'JOIN')) {
      let j = i + 1;
      if (isName(sig[j])) {
        if (sig[j + 1]?.text === '.' && isName(sig[j + 2])) j += 2; // schema.table
        role.set(sig[j], 'table');
        tables.add(name(sig[j]));
        let k = j + 1;
        if (upper(sig[k]) === 'AS') k += 1;
        if (isName(sig[k])) {
          role.set(sig[k], 'alias');
          aliases.add(name(sig[k]));
        }
      }
    }
    // WITH name AS ( ...  and  , name AS ( ...   (common table expressions)
    if (isName(t) && upper(sig[i + 1]) === 'AS' && sig[i + 2]?.text === '(' && (upper(sig[i - 1]) === 'WITH' || sig[i - 1]?.text === ',')) {
      role.set(t, 'table');
      tables.add(name(t));
    }
    // <expr> AS <alias>   (column aliases)
    if (upper(t) === 'AS' && isName(sig[i + 1]) && !role.has(sig[i + 1])) {
      role.set(sig[i + 1], 'alias');
      aliases.add(name(sig[i + 1]));
    }
  }

  for (let i = 0; i < sig.length; i++) {
    const t = sig[i];
    if (!isName(t) || role.has(t)) continue;
    const next = sig[i + 1];
    if (next?.text === '(' && t.kind === 'ident') role.set(t, 'function');
    else if (next?.text === '.' && isName(sig[i + 2]) || next?.text === '.' && sig[i + 2]?.text === '*') role.set(t, tables.has(name(t)) && !aliases.has(name(t)) ? 'table' : 'alias');
    else if (sig[i - 1]?.text === '.') role.set(t, 'column');
    else if (aliases.has(name(t))) role.set(t, 'alias');
    else if (tables.has(name(t))) role.set(t, 'table');
    else role.set(t, 'column');
  }

  for (const t of raw) {
    if (t.kind === 'ident' || t.kind === 'quoted') t.kind = role.get(t) ?? 'column';
  }
}

/** Split formatted SQL into indented lines of classified tokens for rendering. */
export function highlightSql(formatted: string): SqlLine[] {
  const raw = lex(formatted);
  classify(raw);
  const lines: SqlLine[] = [];
  let current: SqlToken[] = [];
  let indent = 0;
  let param = 0;
  const flush = () => {
    if (current.length) lines.push({ indent, tokens: current });
    current = [];
    indent = 0;
  };
  for (const t of raw) {
    if (t.kind === 'ws') {
      const parts = t.text.split('\n');
      if (parts.length > 1) {
        flush();
        indent = parts[parts.length - 1].length; // spaces after the last newline
      } else if (current.length === 0) {
        indent += t.text.length; // leading spaces of the first line
      } else {
        current.push({ text: ' ', kind: 'text' });
      }
      continue;
    }
    current.push({ text: t.text, kind: t.kind as TokenKind, ...(t.kind === 'param' ? { param: param++ } : {}) });
  }
  flush();
  return lines;
}
