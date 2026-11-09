import { FlaskConical } from 'lucide-react';
import { useEffect, useState } from 'react';
import { ApiError, api } from '../api/client';
import type { ScenarioOptions, ScenarioResult } from '../api/types';
import { ScenarioPanel } from './ScenarioPanel';
import { ErrorNotice, Skeleton } from './ui';

const CUSTOM = 'custom';

/**
 * Scenario controls for the Exposure workspace: pick a predefined scenario or enter custom shocks (market-wide and/or
 * one sector), then run it for the selected portfolio. Everything is computed and verified by the backend.
 */
export function ScenarioWorkbench({ portfolioName }: { portfolioName: string }) {
  const [options, setOptions] = useState<ScenarioOptions | null>(null);
  const [choice, setChoice] = useState<string>('broad_market_downturn');
  const [market, setMarket] = useState('');
  const [sector, setSector] = useState('');
  const [sectorShock, setSectorShock] = useState('');
  const [state, setState] = useState<{ status: 'idle' | 'loading' | 'error' | 'done'; result?: ScenarioResult; message?: string }>({ status: 'idle' });

  useEffect(() => {
    let active = true;
    api.scenarioOptions().then((o) => active && setOptions(o && Array.isArray(o.scenarios) && Array.isArray(o.sectors) ? o : null)).catch(() => active && setOptions(null));
    return () => {
      active = false;
    };
  }, []);

  // A result belongs to one portfolio: clear it when the selected portfolio changes.
  useEffect(() => setState({ status: 'idle' }), [portfolioName]);

  const limits = options?.shock_limits_pct ?? { min: -100, max: 100 };
  const customValid = choice !== CUSTOM || market.trim() !== '' || (sector !== '' && sectorShock.trim() !== '');

  const run = async () => {
    setState({ status: 'loading' });
    try {
      const body: { portfolio_name: string; scenario_id?: string; market_shock_pct?: number; sector_shocks?: Record<string, number> } = { portfolio_name: portfolioName };
      if (choice === CUSTOM) {
        if (market.trim() !== '') body.market_shock_pct = Number(market);
        if (sector !== '' && sectorShock.trim() !== '') body.sector_shocks = { [sector]: Number(sectorShock) };
      } else {
        body.scenario_id = choice;
      }
      setState({ status: 'done', result: await api.scenario(body) });
    } catch (err) {
      setState({ status: 'error', message: err instanceof ApiError ? err.message : 'The scenario could not be run.' });
    }
  };

  return (
    <section aria-label="Scenario analysis" className="mt-10 space-y-5">
      <div>
        <h2 className="flex items-center gap-2 text-headline-lg font-bold text-indigo-800"><FlaskConical size={22} aria-hidden="true" /> Scenario analysis</h2>
        <p className="mt-1 max-w-3xl text-body-md text-text-secondary">
          Illustrate how hypothetical sector moves could affect <strong>{portfolioName}</strong>&rsquo;s recorded equity weights. Results are
          hypothetical, not forecasts and not actual performance; bonds are excluded and weights are not renormalised.
        </p>
      </div>
      <form
        className="card flex flex-col gap-4 p-5"
        onSubmit={(e) => {
          e.preventDefault();
          if (customValid && portfolioName) void run();
        }}
      >
        <div className="flex flex-col gap-3 md:flex-row md:items-end">
          <label className="flex flex-col gap-1 text-label-md uppercase tracking-wider text-indigo-900">
            Scenario
            <select value={choice} onChange={(e) => setChoice(e.target.value)} className="h-11 min-w-[260px] rounded-full bg-lavender-input px-4 text-label-lg font-semibold normal-case tracking-normal text-indigo-850">
              {(options?.scenarios ?? []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              <option value={CUSTOM}>Custom shocks…</option>
            </select>
          </label>
          {choice === CUSTOM && (
            <>
              <label className="flex flex-col gap-1 text-label-md uppercase tracking-wider text-indigo-900">
                Market-wide shock (%)
                <input type="number" step="any" min={limits.min} max={limits.max} value={market} onChange={(e) => setMarket(e.target.value)} placeholder="e.g. -15" className="h-11 w-40 rounded-full bg-lavender-input px-4 text-label-lg normal-case tracking-normal" />
              </label>
              <label className="flex flex-col gap-1 text-label-md uppercase tracking-wider text-indigo-900">
                Sector
                <select value={sector} onChange={(e) => setSector(e.target.value)} className="h-11 min-w-[200px] rounded-full bg-lavender-input px-4 text-label-lg normal-case tracking-normal">
                  <option value="">None</option>
                  {(options?.sectors ?? []).map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-label-md uppercase tracking-wider text-indigo-900">
                Sector shock (%)
                <input type="number" step="any" min={limits.min} max={limits.max} value={sectorShock} onChange={(e) => setSectorShock(e.target.value)} placeholder="e.g. -25" className="h-11 w-40 rounded-full bg-lavender-input px-4 text-label-lg normal-case tracking-normal" />
              </label>
            </>
          )}
          <button type="submit" disabled={!customValid || !portfolioName || state.status === 'loading'} className="flex h-11 items-center justify-center gap-2 rounded-full px-7 text-label-lg font-semibold text-on-primary shadow-md hover:brightness-110 disabled:opacity-50" style={{ background: 'linear-gradient(144deg, #4C4CCC 40.85%, #0000A0 98.66%)' }}>
            <span>Run scenario</span>
          </button>
        </div>
        {choice !== CUSTOM && options && <p className="text-body-sm text-text-muted">{options.scenarios.find((s) => s.id === choice)?.description}</p>}
        {choice === CUSTOM && <p className="text-body-sm text-text-muted">Shocks are percentages between {limits.min}% and +{limits.max}%; a negative number is a fall.</p>}
      </form>
      {state.status === 'loading' && <div className="card p-6"><Skeleton lines={5} /></div>}
      {state.status === 'error' && <ErrorNotice message={state.message ?? 'The scenario could not be run.'} />}
      {state.status === 'done' && state.result && <ScenarioPanel result={state.result} />}
    </section>
  );
}
