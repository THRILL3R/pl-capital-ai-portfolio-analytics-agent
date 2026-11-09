import type { DonutSegment } from '../lib/exposure';
import { formatPercent } from '../lib/format';

const RADIUS = 45;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/** Donut chart: each segment's length is its percentage of 100. */
export function Donut({ segments, centerValue, centerLabel }: { segments: DonutSegment[]; centerValue: string; centerLabel: string }) {
  let offset = 0;
  const description = segments.map((s) => `${s.label} ${formatPercent(s.pct)}`).join(', ');
  return (
    <div className="relative h-48 w-48 shrink-0">
      <svg viewBox="0 0 120 120" className="h-full w-full -rotate-90" role="img" aria-label={`Exposure composition: ${description}`}>
        <circle cx="60" cy="60" r={RADIUS} fill="none" stroke="#F0F0F0" strokeWidth="18" />
        {segments.map((s) => {
          const length = Math.min(100, Math.max(0, s.pct)) / 100 * CIRCUMFERENCE;
          const node = (
            <circle
              key={s.label}
              cx="60"
              cy="60"
              r={RADIUS}
              fill="none"
              stroke={s.color}
              strokeWidth="18"
              strokeDasharray={`${length} ${CIRCUMFERENCE}`}
              strokeDashoffset={-offset}
            />
          );
          offset += length;
          return node;
        })}
      </svg>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
        <span className="tabular text-headline-md font-extrabold leading-none text-indigo-900">{centerValue}</span>
        <span className="mt-1 text-label-sm font-semibold uppercase text-text-muted">{centerLabel}</span>
      </div>
    </div>
  );
}

export function Legend({ segments }: { segments: DonutSegment[] }) {
  return (
    <ul className="flex min-w-[200px] flex-col gap-3">
      {segments.map((s) => (
        <li key={s.label} className={`flex items-center justify-between text-body-sm font-medium ${s.excluded ? 'mt-1 pt-2' : ''}`}>
          <span className="flex items-center gap-2.5">
            <span className="h-3.5 w-3.5 shrink-0 rounded-sm" style={{ background: s.color }} aria-hidden="true" />
            <span className={s.excluded ? 'text-text-secondary' : 'text-text-primary'}>{s.label}</span>
          </span>
          <span className={`tabular ml-4 text-label-md font-bold ${s.excluded ? 'text-text-muted' : 'text-indigo-900'}`}>{formatPercent(s.pct)}</span>
        </li>
      ))}
    </ul>
  );
}

export function BarRow({ label, valueText, pct, color, caption }: { label: string; valueText: string; pct: number; color: string; caption?: string }) {
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-label-md font-semibold text-text-primary">{label}</span>
        <span className="tabular text-label-md font-bold text-indigo-800">{valueText}</span>
      </div>
      <div className="h-3 w-full overflow-hidden rounded-full bg-surface-container" role="presentation">
        <div className="h-full rounded-full transition-all duration-700" style={{ width: `${Math.min(100, Math.max(0, pct))}%`, background: color }} />
      </div>
      {caption && <p className="mt-1 text-label-sm text-text-muted">{caption}</p>}
    </div>
  );
}

/** NAV sparkline from real records. Values are plotted as stored. */
export function Sparkline({ values, label }: { values: number[]; label: string }) {
  if (values.length < 2) return null;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const points = values.map((v, i) => `${(i / (values.length - 1)) * 500},${60 - ((v - min) / span) * 52}`);
  return (
    <svg viewBox="0 0 500 64" className="h-16 w-full text-primary-container" role="img" aria-label={label} preserveAspectRatio="none">
      <polyline points={points.join(' ')} fill="none" stroke="currentColor" strokeWidth="2.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
