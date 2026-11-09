import { ArrowRight, DatabaseZap } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError, api } from '../api/client';
import type { ExposureResult, PerformanceRecord, PortfolioDetail, PortfolioSummary, RiskRecord } from '../api/types';
import { BarRow, Sparkline } from '../components/charts';
import { Disclaimer, ErrorNotice, NoData, PageHeader, Pill, Skeleton } from '../components/ui';
import { colorFor } from '../lib/exposure';
import { formatCurrency, formatNumber, formatPercent } from '../lib/format';

type Filter = 'all' | 'active' | 'passive' | 'high';
type Tab = 'holdings' | 'exposure' | 'performance' | 'risk';

const FILTERS: { id: Filter; label: string; test: (p: PortfolioSummary) => boolean }[] = [
  { id: 'all', label: 'All', test: () => true },
  { id: 'active', label: 'Active', test: (p) => p.status === 'Active' },
  { id: 'passive', label: 'Passive', test: (p) => p.status === 'Passive' },
  { id: 'high', label: 'High risk', test: (p) => p.target_risk_level === 'High' },
];

const PERFORMANCE_FIELDS: { key: keyof PerformanceRecord; label: string }[] = [
  { key: 'nav', label: 'NAV' },
  { key: 'total_return_1m', label: 'Return 1M' },
  { key: 'total_return_3m', label: 'Return 3M' },
  { key: 'total_return_6m', label: 'Return 6M' },
  { key: 'total_return_1y', label: 'Return 1Y' },
  { key: 'volatility', label: 'Volatility' },
  { key: 'sharpe_ratio', label: 'Sharpe ratio' },
  { key: 'max_drawdown', label: 'Max drawdown' },
];

const RISK_FIELDS: { key: keyof RiskRecord; label: string }[] = [
  { key: 'var_95', label: 'VaR 95' },
  { key: 'var_99', label: 'VaR 99' },
  { key: 'cvar_95', label: 'CVaR 95' },
  { key: 'beta', label: 'Beta' },
  { key: 'correlation_sp500', label: 'Correlation to S&P 500' },
  { key: 'tracking_error', label: 'Tracking error' },
  { key: 'information_ratio', label: 'Information ratio' },
  { key: 'sortino_ratio', label: 'Sortino ratio' },
];

const num = (v: unknown) => (typeof v === 'number' ? formatNumber(v) : '—');

function Chip({ children }: { children: string }) {
  return <Pill tone="neutral">{children}</Pill>;
}

function RecordSeries<T extends object>({ records, dateKey, fields, title, extra }: { records: T[]; dateKey: keyof T; fields: { key: keyof T; label: string }[]; title: string; extra?: React.ReactNode }) {
  const latest = records[records.length - 1] as Record<string, unknown>;
  return (
    <div className="flex flex-col gap-4">
      <div className="rounded-lg bg-surface-container-low p-4">
        <span className="text-label-sm uppercase text-text-muted">{records.length} dated records</span>
        <p className="mt-1 text-headline-sm text-indigo-900">{title} · latest {String(latest[dateKey as string])}</p>
        <p className="mt-1 text-body-sm text-text-secondary">Values are shown exactly as stored. The database schema does not document their units.</p>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {fields.map((f) => (
          <div key={String(f.key)} className="rounded-lg border border-border-hairline bg-surface-base p-3">
            <div className="text-label-sm uppercase text-text-muted">{f.label}</div>
            <div className="tabular text-headline-md font-bold text-violet-accent">{num(latest[f.key as string])}</div>
          </div>
        ))}
      </div>
      {extra}
      <div className="overflow-x-auto rounded-lg border border-border-hairline" role="region" aria-label={`${title} records`} tabIndex={0}>
        <table className="w-full text-left text-table-cell">
          <thead>
            <tr className="table-head">
              <th scope="col" className="px-4 py-2.5">Date</th>
              {fields.map((f) => <th key={String(f.key)} scope="col" className="px-4 py-2.5 text-right">{f.label}</th>)}
            </tr>
          </thead>
          <tbody className="divide-y divide-border-table-row">
            {records.map((r) => {
              const row = r as Record<string, unknown>;
              return (
                <tr key={String(row[dateKey as string])}>
                  <td className="px-4 py-2.5 font-semibold text-indigo-800">{String(row[dateKey as string])}</td>
                  {fields.map((f) => <td key={String(f.key)} className="tabular px-4 py-2.5 text-right">{num(row[f.key as string])}</td>)}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ExposureTab({ name }: { name: string }) {
  const [state, setState] = useState<{ s: 'loading' | 'done' | 'error'; r?: ExposureResult; m?: string }>({ s: 'loading' });
  useEffect(() => {
    let active = true;
    setState({ s: 'loading' });
    api.exposure(name).then((r) => active && setState({ s: 'done', r })).catch((e) => active && setState({ s: 'error', m: e instanceof ApiError ? e.message : 'Failed.' }));
    return () => { active = false; };
  }, [name]);
  if (state.s === 'loading') return <Skeleton lines={4} />;
  if (state.s === 'error' || !state.r) return <ErrorNotice message={state.m ?? 'Could not calculate exposure.'} />;
  const r = state.r;
  if (r.status === 'no_equity_holdings') return <NoData title="No equity holdings">There are no stock holdings mapped to a sector in this portfolio.</NoData>;
  if (r.status === 'error') return <ErrorNotice message={r.error ?? 'Could not calculate exposure.'} />;
  return (
    <div className="flex flex-col gap-4 rounded-lg bg-surface-container-low p-5">
      <p className="text-body-md text-text-secondary">
        Total equity weight <strong className="tabular text-indigo-900">{formatPercent(r.total_equity_weight_pct)}</strong> across {r.equity_holdings_count} equity holdings; {r.excluded_bond_holdings} bond holdings excluded.
      </p>
      {r.exposures.map((e, i) => <BarRow key={e.sector} label={e.sector} valueText={formatPercent(e.exposure_pct)} pct={e.exposure_pct} color={colorFor(i)} />)}
      <Link to={`/exposure?portfolio=${encodeURIComponent(name)}`} className="text-label-md text-violet-accent hover:text-brand-blue">Open the full Exposure workspace →</Link>
    </div>
  );
}

function Detail({ summary }: { summary: PortfolioSummary }) {
  const [detail, setDetail] = useState<PortfolioDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('holdings');
  useEffect(() => {
    let active = true;
    setDetail(null);
    setError(null);
    api.portfolio(summary.portfolio_id).then((d) => active && setDetail(d)).catch((e) => active && setError(e instanceof ApiError ? e.message : 'Could not load details.'));
    return () => { active = false; };
  }, [summary.portfolio_id]);

  const tabs: { id: Tab; label: string }[] = [
    { id: 'holdings', label: 'Holdings' },
    { id: 'exposure', label: 'Exposure' },
    { id: 'performance', label: 'Performance' },
    { id: 'risk', label: 'Risk' },
  ];
  return (
    <section className="card border-l-4 border-violet-accent p-5 shadow-md sm:p-6" aria-label={`Details: ${summary.portfolio_name}`}>
      <div className="mb-5 flex flex-col gap-4 border-b border-border-hairline pb-4 md:flex-row md:items-center md:justify-between">
        <div>
          <div className="mb-1 flex items-center gap-2 text-label-sm font-bold uppercase tracking-wider text-violet-accent">
            Selected portfolio <span className="text-text-muted">/</span>
            <span className="font-normal normal-case tracking-normal text-text-muted">{summary.status} · {summary.strategy_type} strategy</span>
          </div>
          <h2 className="text-headline-md font-bold text-indigo-900">{summary.portfolio_name}</h2>
        </div>
        <dl className="flex flex-wrap items-center gap-4 rounded-lg bg-surface-container-low px-4 py-2.5">
          {([['AUM', formatCurrency(summary.total_aum)], ['Strategy', summary.strategy_type], ['Target risk', summary.target_risk_level], ['Status', summary.status]] as const).map(([k, v], i) => (
            <div key={k} className={`${i > 0 ? 'border-l border-surface-container-highest pl-4' : ''}`}>
              <dt className="text-label-sm text-text-muted">{k}</dt>
              <dd className="tabular text-headline-sm font-bold text-indigo-900">{v}</dd>
            </div>
          ))}
        </dl>
      </div>

      <div role="tablist" aria-label="Portfolio detail" className="mb-6 inline-flex flex-wrap gap-2 rounded-lg bg-lavender-callout p-1">
        {tabs.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id} type="button" onClick={() => setTab(t.id)} className={`rounded px-4 py-1.5 text-label-md transition-all ${tab === t.id ? 'bg-surface-base font-semibold text-indigo-900 shadow-sm' : 'text-text-secondary hover:text-indigo-900'}`}>
            {t.label}
          </button>
        ))}
      </div>

      {error && <ErrorNotice message={error} />}
      {!error && !detail && <Skeleton lines={5} />}
      {detail && (
        <div role="tabpanel">
          {tab === 'holdings' && (
            <div>
              <div className="mb-3 flex items-center justify-between">
                <h3 className="text-headline-sm text-indigo-900">Holdings by cost basis</h3>
                <span className="text-body-sm text-text-muted">{detail.holdings.length} holdings</span>
              </div>
              <div className="overflow-x-auto rounded-lg border border-border-hairline" role="region" aria-label="Holdings" tabIndex={0}>
                <table className="w-full text-left text-table-cell">
                  <thead>
                    <tr className="table-head">
                      {['Ticker', 'Company', 'Sector', 'Quantity', 'Cost basis', 'Current price', 'Weight'].map((h, i) => <th key={h} scope="col" className={`px-4 py-2.5 ${i >= 3 ? 'text-right' : ''}`}>{h}</th>)}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border-table-row">
                    {detail.holdings.map((h) => (
                      <tr key={h.symbol} className="transition-colors hover:bg-surface-container-low">
                        <td className="px-4 py-3 font-bold text-brand-blue">{h.symbol}</td>
                        <td className="px-4 py-3 text-text-primary">{h.company_name}</td>
                        <td className="px-4 py-3 text-text-secondary">{h.sector ?? (h.asset_type === 'Bond' ? 'Bond (no sector)' : '—')}</td>
                        <td className="tabular px-4 py-3 text-right">{formatNumber(h.quantity)}</td>
                        <td className="tabular px-4 py-3 text-right font-medium">{formatCurrency(h.cost_basis)}</td>
                        <td className="tabular px-4 py-3 text-right">{formatCurrency(h.current_price)}</td>
                        <td className="tabular px-4 py-3 text-right text-text-secondary">{formatPercent(h.current_weight * 100)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          {tab === 'exposure' && <ExposureTab name={summary.portfolio_name} />}
          {tab === 'performance' &&
            (detail.performance.length === 0 ? (
              <NoData title="No data available" icon={<DatabaseZap size={32} aria-hidden="true" />}>This sample dataset holds no performance records for {summary.portfolio_name}.</NoData>
            ) : (
              <RecordSeries records={detail.performance} dateKey="performance_date" fields={PERFORMANCE_FIELDS} title="Performance" extra={
                <div className="rounded-lg border border-border-hairline bg-surface-base p-4">
                  <div className="mb-2 text-label-sm font-medium text-text-muted">NAV, {detail.performance.length} dated records</div>
                  <Sparkline values={detail.performance.map((p) => p.nav)} label={`NAV from ${detail.performance[0].performance_date} to ${detail.performance[detail.performance.length - 1].performance_date}`} />
                </div>
              } />
            ))}
          {tab === 'risk' &&
            (detail.risk.length === 0 ? (
              <NoData title="No data available" icon={<DatabaseZap size={32} aria-hidden="true" />}>This sample dataset holds no risk records for {summary.portfolio_name}.</NoData>
            ) : (
              <RecordSeries records={detail.risk} dateKey="calculation_date" fields={RISK_FIELDS} title="Risk metrics" />
            ))}
        </div>
      )}
    </section>
  );
}

export function Portfolios() {
  const [rows, setRows] = useState<PortfolioSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>('all');
  const [selectedId, setSelectedId] = useState<number | null>(null);

  useEffect(() => {
    let active = true;
    api.portfolios().then((r) => { if (active) { setRows(r); setSelectedId(r[0]?.portfolio_id ?? null); } }).catch((e) => active && setError(e instanceof ApiError ? e.message : 'Could not load portfolios.'));
    return () => { active = false; };
  }, []);

  const visible = useMemo(() => (rows ?? []).filter(FILTERS.find((f) => f.id === filter)!.test), [rows, filter]);
  const totalAum = (list: PortfolioSummary[]) => list.reduce((s, p) => s + p.total_aum, 0);
  const selected = rows?.find((p) => p.portfolio_id === selectedId) ?? null;

  return (
    <>
      <PageHeader crumb="Portfolios" eyebrow="Sample dataset explorer" title="Portfolio Directory & Screener">
        <span className="text-body-sm text-text-muted">
          Read-only view of the local database, loaded with a fixed query:{' '}
          <code className="rounded bg-lavender-callout px-1.5 py-0.5 font-mono text-indigo-900">SELECT portfolio_id, portfolio_name, strategy_type, target_risk_level, status, total_aum FROM portfolios</code>
        </span>
      </PageHeader>

      {error && <ErrorNotice message={error} />}
      {!error && !rows && <div className="card p-6"><Skeleton lines={8} /></div>}
      {rows && (
        <>
          <div className="mb-6 flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div role="group" aria-label="Filter portfolios" className="inline-flex flex-wrap items-center gap-1 self-start rounded-full bg-surface-container-low p-1 shadow-sm">
              {FILTERS.map((f) => (
                <button key={f.id} type="button" aria-pressed={filter === f.id} onClick={() => setFilter(f.id)} className={`rounded-full px-4 py-1.5 text-label-md transition-all ${filter === f.id ? 'bg-brand-blue text-on-primary shadow-sm' : 'text-indigo-800 hover:bg-lavender-input'}`}>
                  {f.label} ({rows.filter(f.test).length})
                </button>
              ))}
            </div>
            <div className="self-start rounded-full bg-surface-base px-4 py-2 text-label-md text-indigo-900 shadow-sm lg:self-auto">
              Total AUM: <strong className="tabular font-bold text-violet-accent">{formatCurrency(totalAum(rows))}</strong>
              <span className="mx-2 text-text-muted">·</span>
              <span className="text-body-sm text-text-secondary">{rows.length} portfolios</span>
            </div>
          </div>

          <div className="card mb-8 overflow-hidden">
            <div className="overflow-x-auto" role="region" aria-label="Portfolios" tabIndex={0}>
              <table className="w-full text-left" id="portfolios-table">
                <thead>
                  <tr className="table-head">
                    {['Portfolio name', 'Strategy', 'Target risk', 'Status', 'AUM ($)', 'Action'].map((h, i) => (
                      <th key={h} scope="col" className={`px-5 py-3.5 tracking-wider ${i === 4 ? 'text-right' : i === 5 ? 'text-center' : ''}`}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-surface-container">
                  {visible.map((p) => {
                    const isSel = p.portfolio_id === selectedId;
                    return (
                      <tr key={p.portfolio_id} onClick={() => setSelectedId(p.portfolio_id)} aria-selected={isSel} className={`cursor-pointer transition-colors ${isSel ? 'bg-lavender-callout/70' : 'hover:bg-surface-container-low'}`}>
                        <td className="px-5 py-3.5">
                          <span className="flex items-center gap-2">
                            <span className={`inline-block h-4 w-1.5 rounded-full ${isSel ? 'bg-violet-accent' : 'bg-transparent'}`} aria-hidden="true" />
                            <span className="text-[14px] font-semibold text-indigo-900">{p.portfolio_name}</span>
                          </span>
                        </td>
                        <td className="px-5 py-3.5 text-table-cell text-text-secondary">{p.strategy_type}</td>
                        <td className="px-5 py-3.5"><Chip>{p.target_risk_level}</Chip></td>
                        <td className="px-5 py-3.5"><Chip>{p.status}</Chip></td>
                        <td className="tabular px-5 py-3.5 text-right text-table-cell font-medium">{formatCurrency(p.total_aum)}</td>
                        <td className="px-5 py-3.5 text-center">
                          <button type="button" onClick={(e) => { e.stopPropagation(); setSelectedId(p.portfolio_id); document.getElementById('portfolio-detail')?.scrollIntoView({ behavior: 'smooth' }); }} className="inline-flex items-center gap-0.5 text-label-md text-violet-accent hover:text-brand-blue">
                            View details <ArrowRight size={16} aria-hidden="true" />
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="flex flex-col justify-between gap-2 border-t border-border-hairline bg-surface-container-low px-5 py-3 sm:flex-row sm:items-center">
              <span className="tabular text-body-sm font-medium text-text-secondary">Total AUM {formatCurrency(totalAum(visible))} (sum of the {visible.length} rows shown)</span>
              <span className="text-label-sm text-text-muted">{visible.length} records matching criteria</span>
            </div>
          </div>

          <div id="portfolio-detail">{selected && <Detail summary={selected} />}</div>
        </>
      )}
      <Disclaimer />
    </>
  );
}
