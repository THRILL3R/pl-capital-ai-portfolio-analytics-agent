import { Calculator } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ApiError, api } from '../api/client';
import type { ExposureResult, Holding, PortfolioSummary } from '../api/types';
import { ExposureCharts, ExposureKpis, VerificationStatus } from '../components/ExposurePanels';
import { ScenarioWorkbench } from '../components/ScenarioWorkbench';
import { Disclaimer, ErrorNotice, NoData, PageHeader, Pill, SegmentedToggle, Skeleton } from '../components/ui';
import { formatPercent } from '../lib/format';

const DEFAULT_PORTFOLIO = 'Balanced Portfolio';

export function HoldingsWeightTable({ holdings }: { holdings: Holding[] }) {
  const sorted = [...holdings].sort((a, b) => b.current_weight - a.current_weight);
  return (
    <div className="card overflow-hidden p-1">
      <div className="overflow-x-auto" role="region" aria-label="Holdings and inclusion in exposure" tabIndex={0}>
        <table className="w-full text-left">
          <thead className="bg-surface-table-header">
            <tr className="text-table-header uppercase text-text-muted">
              <th scope="col" className="px-4 py-3">Asset name / ticker</th>
              <th scope="col" className="px-4 py-3">Asset class</th>
              <th scope="col" className="px-4 py-3">Sector</th>
              <th scope="col" className="px-4 py-3 text-right">Weight</th>
              <th scope="col" className="px-4 py-3 text-center">Exposure inclusion</th>
            </tr>
          </thead>
          <tbody className="text-table-cell">
            {sorted.map((h) => {
              const included = h.asset_type === 'Stock';
              return (
                <tr key={h.symbol} className={`transition-colors hover:bg-surface-container-low ${included ? '' : 'opacity-75'}`}>
                  <td className={`px-4 py-3.5 font-semibold ${included ? 'text-indigo-900' : 'text-text-muted'}`}>{h.company_name} ({h.symbol})</td>
                  <td className="px-4 py-3.5"><Pill tone={included ? 'indigo' : 'neutral'}>{h.asset_type}</Pill></td>
                  <td className="px-4 py-3.5 text-text-primary">{h.sector ?? '—'}</td>
                  <td className="tabular px-4 py-3.5 text-right font-semibold text-indigo-800">{formatPercent(h.current_weight * 100)}</td>
                  <td className="px-4 py-3.5 text-center"><Pill tone="neutral">{included ? 'Included' : 'Excluded'}</Pill></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function Exposure() {
  const [params, setParams] = useSearchParams();
  const [portfolios, setPortfolios] = useState<PortfolioSummary[] | null>(null);
  const [selected, setSelected] = useState(params.get('portfolio') ?? '');
  const [state, setState] = useState<{ status: 'idle' | 'loading' | 'error' | 'done'; result?: ExposureResult; holdings?: Holding[]; message?: string }>({ status: 'idle' });
  const [view, setView] = useState<'chart' | 'table'>('chart');
  const autoRan = useRef(false);

  const calculate = useCallback(
    async (name: string, list: PortfolioSummary[]) => {
      setState({ status: 'loading' });
      try {
        const match = list.find((p) => p.portfolio_name === name);
        const [result, detail] = await Promise.all([api.exposure(name), match ? api.portfolio(match.portfolio_id) : Promise.resolve(null)]);
        setState({ status: 'done', result, holdings: detail?.holdings });
        setParams({ portfolio: name }, { replace: true });
      } catch (err) {
        setState({ status: 'error', message: err instanceof ApiError ? err.message : 'The calculation failed.' });
      }
    },
    [setParams],
  );

  useEffect(() => {
    let active = true;
    api
      .portfolios()
      .then((list) => {
        if (!active) return;
        setPortfolios(list);
        const initial = list.some((p) => p.portfolio_name === selected) ? selected : list.some((p) => p.portfolio_name === DEFAULT_PORTFOLIO) ? DEFAULT_PORTFOLIO : list[0]?.portfolio_name ?? '';
        setSelected(initial);
        if (initial && !autoRan.current) {
          autoRan.current = true;
          void calculate(initial, list);
        }
      })
      .catch((err) => active && setState({ status: 'error', message: err instanceof ApiError ? err.message : 'Could not load portfolios.' }));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once on mount
  }, []);

  const names = useMemo(() => portfolios?.map((p) => p.portfolio_name) ?? [], [portfolios]);
  const { result, holdings } = state;

  return (
    <>
      <PageHeader crumb="Exposure" eyebrow="Deterministic calculator" title="Sector Exposure Analysis">
        Deterministic equity-only sector exposure for a single named portfolio. Bond holdings are excluded; equity weights are summed as stored and <strong>not</strong> renormalised to 100%.
      </PageHeader>

      <form
        className="card mb-6 flex flex-col justify-between gap-5 p-5 md:flex-row md:items-center"
        onSubmit={(e) => {
          e.preventDefault();
          if (portfolios && selected) void calculate(selected, portfolios);
        }}
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <label htmlFor="portfolio-selector" className="whitespace-nowrap text-label-md uppercase tracking-wider text-indigo-900">Select portfolio:</label>
          <select
            id="portfolio-selector"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            disabled={!portfolios}
            className="h-11 min-w-[260px] cursor-pointer rounded-full bg-lavender-input px-4 text-label-lg font-semibold text-indigo-850 shadow-sm focus:bg-surface-base focus:outline-none"
          >
            {names.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </div>
        <button type="submit" disabled={!selected || state.status === 'loading'} className="flex h-11 items-center justify-center gap-2 rounded-full px-7 text-label-lg font-semibold text-on-primary shadow-md transition-all hover:brightness-110 active:scale-[0.99] disabled:opacity-50" style={{ background: 'linear-gradient(144deg, #4C4CCC 40.85%, #0000A0 98.66%)' }}>
          <Calculator size={18} aria-hidden="true" />
          <span>Calculate exposure</span>
        </button>
      </form>

      {state.status === 'loading' && <div className="card p-6"><Skeleton lines={6} /></div>}
      {state.status === 'error' && <ErrorNotice message={state.message ?? 'Something went wrong.'} onRetry={portfolios && selected ? () => void calculate(selected, portfolios) : undefined} />}

      {state.status === 'done' && result && result.status === 'error' && (
        <div className="flex flex-col gap-3">
          <div><VerificationStatus verification={result.verification} /></div>
          <ErrorNotice message={result.error ?? 'The calculation failed.'} />
        </div>
      )}
      {state.status === 'done' && result && result.status === 'no_equity_holdings' && (
        <NoData title="No equity holdings">
          {result.portfolio_name} has {result.total_holdings_count} holdings, none of them stocks mapped to a sector ({result.excluded_bond_holdings} bond holdings excluded). There is no equity exposure to show.
        </NoData>
      )}
      {state.status === 'done' && result && result.status === 'success' && (
        <div className="flex flex-col gap-6">
          <section className="card flex flex-col justify-between gap-4 p-5 md:flex-row md:items-center">
            <div className="flex flex-wrap items-center gap-3">
              <h2 className="text-headline-lg font-bold text-indigo-800">{result.portfolio_name}</h2>
              <Pill tone="violet">Exposure Calculator</Pill>
              <VerificationStatus verification={result.verification} />
              <Pill tone="neutral">Deterministic · Bonds excluded · Weights not renormalised</Pill>
            </div>
            <SegmentedToggle label="View" value={view} onChange={setView} options={[{ id: 'chart', label: 'Chart & breakdown' }, { id: 'table', label: 'Table view' }]} />
          </section>
          <ExposureKpis result={result} />
          {view === 'chart' ? (
            <ExposureCharts result={result} holdings={holdings} />
          ) : holdings ? (
            <HoldingsWeightTable holdings={holdings} />
          ) : (
            <NoData title="Holdings unavailable">Per-holding weights could not be loaded for this portfolio.</NoData>
          )}
        </div>
      )}
      {state.status === 'done' && result && result.status !== 'error' && <ScenarioWorkbench portfolioName={result.portfolio_name} />}
      <Disclaimer />
    </>
  );
}
