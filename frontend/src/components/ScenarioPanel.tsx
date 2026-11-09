import { FlaskConical, TriangleAlert } from 'lucide-react';
import type { ScenarioResult } from '../api/types';
import { formatCurrency } from '../lib/format';
import { AnswerVerificationStatus, VerificationStatus } from './ExposurePanels';
import { Callout, ErrorNotice, NoData, Pill } from './ui';

const pp = (v: number) => `${v > 0 ? '+' : ''}${v.toFixed(2)} pp`;
const pct = (v: number) => `${v > 0 ? '+' : ''}${v.toFixed(2)}%`;

/**
 * Hypothetical scenario / stress-test result. Always labelled as hypothetical (never a forecast, never actual
 * performance). When the backend withholds the figures (failed or unverified), only the notice is shown.
 */
export function ScenarioPanel({ result }: { result: ScenarioResult }) {
  const verification = result.verification;
  const shown = result.status === 'success' || result.status === 'no_equity_holdings';
  const scenario = result.scenario;
  return (
    <section aria-label="Scenario analysis (hypothetical)" className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="flex items-center gap-2 text-headline-sm text-indigo-800">
          <FlaskConical size={20} aria-hidden="true" />
          {scenario ? scenario.name : 'Scenario analysis'}
          {result.portfolio_name ? <span className="font-normal text-text-secondary">· {result.portfolio_name}</span> : null}
        </h3>
        <Pill tone="violet">Hypothetical</Pill>
        {result.answer_verification?.status === 'failed' && verification?.status === 'verified'
          ? <AnswerVerificationStatus verification={result.answer_verification} />   /* the answer check overrides a stale 'verified' */
          : <VerificationStatus verification={verification} />}
      </div>

      {!shown && <ErrorNotice message={result.error ?? 'The scenario could not be completed.'} />}

      {shown && scenario && (
        <>
          <p className="text-body-md text-text-secondary">{scenario.description}</p>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <div className="card p-4">
              <div className="text-label-sm font-semibold uppercase tracking-wider text-text-muted">Estimated portfolio impact</div>
              <div className="tabular mt-1 whitespace-nowrap text-[26px] font-bold leading-tight text-indigo-800 xl:text-[30px]" aria-label="Estimated portfolio impact">{result.estimated_portfolio_impact_pct !== null ? pp(result.estimated_portfolio_impact_pct) : '—'}</div>
              <div className="text-body-sm text-text-muted">percentage points of total portfolio value · hypothetical</div>
            </div>
            <div className="card p-4">
              <div className="text-label-sm font-semibold uppercase tracking-wider text-text-muted">Estimated value impact</div>
              {result.estimated_value_impact ? (
                <>
                  <div className="tabular mt-1 whitespace-nowrap text-[26px] font-bold leading-tight text-indigo-800 xl:text-[30px]">{`${result.estimated_value_impact.amount < 0 ? '−' : '+'}${formatCurrency(Math.abs(result.estimated_value_impact.amount))}`}</div>
                  <div className="text-body-sm text-text-muted">on recorded AUM of {formatCurrency(result.estimated_value_impact.aum)} ({result.estimated_value_impact.currency})</div>
                </>
              ) : (
                <div className="mt-1 text-body-md text-text-muted">{result.value_impact_note ?? 'Not available.'}</div>
              )}
            </div>
            <div className="card p-4">
              <div className="text-label-sm font-semibold uppercase tracking-wider text-text-muted">Exposure coverage</div>
              {result.coverage && (
                <dl className="mt-1 space-y-0.5 text-body-sm">
                  <div className="flex justify-between gap-2"><dt className="text-text-muted">Equity exposure shocked</dt><dd className="tabular font-semibold">{result.coverage.shocked_weight_pct.toFixed(2)}%</dd></div>
                  <div className="flex justify-between gap-2"><dt className="text-text-muted">Equity exposure unchanged</dt><dd className="tabular font-semibold">{result.coverage.unshocked_mapped_weight_pct.toFixed(2)}%</dd></div>
                  <div className="flex justify-between gap-2"><dt className="text-text-muted">Unmapped stock holdings ({result.coverage.unmapped_equity_holdings})</dt><dd className="tabular font-semibold">{result.coverage.unmapped_equity_weight_pct.toFixed(2)}%</dd></div>
                  <div className="flex justify-between gap-2"><dt className="text-text-muted">Bonds excluded ({result.coverage.excluded_bond_holdings})</dt><dd className="tabular font-semibold">{result.coverage.excluded_bond_weight_pct.toFixed(2)}%</dd></div>
                </dl>
              )}
            </div>
          </div>

          {result.status === 'no_equity_holdings' ? (
            <NoData title="No equity holdings">There is no mapped equity exposure in this portfolio, so these shocks have nothing to act on.</NoData>
          ) : (
            <div className="card overflow-hidden p-1">
              <div className="overflow-x-auto" role="region" aria-label="Sector-level scenario impact" tabIndex={0}>
                <table className="w-full text-left">
                  <caption className="sr-only">Sector-level hypothetical impact: recorded exposure times shock</caption>
                  <thead className="bg-surface-table-header">
                    <tr className="text-table-header uppercase text-text-muted">
                      <th scope="col" className="px-2 py-3 sm:px-4">Sector</th>
                      <th scope="col" className="px-2 py-3 sm:px-4 text-right">Exposure</th>
                      <th scope="col" className="px-2 py-3 sm:px-4 text-right"><span aria-hidden="true" className="sm:hidden">Shock</span><span className="sr-only sm:not-sr-only">Hypothetical shock</span></th>
                      <th scope="col" className="px-2 py-3 sm:px-4 text-right"><span aria-hidden="true" className="sm:hidden">Impact</span><span className="sr-only sm:not-sr-only">Contribution</span></th>
                    </tr>
                  </thead>
                  <tbody className="text-table-cell">
                    {result.sector_impacts.map((row) => (
                      <tr key={row.sector} className="hover:bg-surface-container-low">
                        <td className="px-2 py-3 sm:px-4 font-semibold text-indigo-900">{row.sector}</td>
                        <td className="tabular px-2 py-3 sm:px-4 text-right">{row.exposure_pct.toFixed(2)}%</td>
                        <td className="tabular px-2 py-3 sm:px-4 text-right">{pct(row.shock_pct)}</td>
                        <td className="tabular px-2 py-3 sm:px-4 text-right font-semibold text-indigo-800">{pp(row.contribution_pp)}</td>
                      </tr>
                    ))}
                  </tbody>
                  <tfoot>
                    <tr className="border-t border-border-hairline font-bold text-indigo-900">
                      <td className="px-2 py-3 sm:px-4" colSpan={3}>Estimated total impact (not renormalised)</td>
                      <td className="tabular px-2 py-3 sm:px-4 text-right">{result.estimated_portfolio_impact_pct !== null ? pp(result.estimated_portfolio_impact_pct) : '—'}</td>
                    </tr>
                  </tfoot>
                </table>
              </div>
            </div>
          )}

          {result.unheld_sector_shocks.length > 0 && (
            <Callout title="Shocks on sectors this portfolio does not hold">
              {result.unheld_sector_shocks.map((u) => `${u.sector} ${pct(u.shock_pct)}`).join(', ')}: no effect on this portfolio.
            </Callout>
          )}

          <details className="rounded-lg bg-surface-container-low p-4">
            <summary className="cursor-pointer text-label-md font-semibold text-indigo-800">Assumptions</summary>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-body-sm text-text-secondary">
              {result.assumptions.map((a) => <li key={a}>{a}</li>)}
            </ul>
          </details>
        </>
      )}

      <p role="note" className="flex items-start gap-2 text-body-sm italic text-text-muted">
        <TriangleAlert size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
        <span>{result.disclaimer}</span>
      </p>
    </section>
  );
}
