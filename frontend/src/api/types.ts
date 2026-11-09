/**
 * Types mirror the EXISTING Python response contracts (portfolio_agent/agent.py and
 * portfolio_agent/tools.py). Nothing here is invented: every field is returned by the backend.
 */

export type AgentStatus = 'success' | 'unsupported' | 'error' | 'no_equity_holdings' | 'conversation' | 'partial';
export type ToolName = 'sql_query' | 'exposure_calculator' | 'hybrid_analysis' | 'scenario_analysis';
export type Row = Record<string, unknown>;

/**
 * Final-answer verification added by the backend (portfolio_agent/answer_verifier.py): is the response consistent
 * with its structured tool results? `verified` | `failed` (inconsistency found; affected data withheld) |
 * `unverified` (could not be completed or evidence missing) | `not_applicable`. Model-written text is never vouched for:
 * it is listed under `unverified_claims`.
 */
export interface AnswerVerification {
  status: 'verified' | 'failed' | 'unverified' | 'not_applicable';
  verified: boolean;
  method: string;
  scope: string;
  checks_run: number;
  mismatch_count: number;
  mismatches: { code: string; message: string; component?: string }[];
  checks_skipped: string[];
  unverified_claims: string[];
  /** Deterministic one-line description derived from the tool result (counts and names only). */
  summary: string | null;
  error: string | null;
}

export interface SqlResult {
  status: string;
  tool: 'sql_query';
  original_question: string;
  generated_sql: string | null;
  sql?: string | null;
  query_parameters: unknown[];
  row_count: number;
  rows: Row[];
  columns: string[];
  generation_method: 'gemini' | 'rule_based' | string | null;
  model_name: string | null;
  fallback_used: boolean;
  fallback_reason: string | null;
  query_description: string | null;
  error: string | null;
  answer_verification?: AnswerVerification;
}

export interface ExposureItem {
  sector: string;
  weight_fraction: number;
  exposure_pct: number;
  holding_count: number;
}

/**
 * Outcome of the backend's independent recomputation of an exposure result. `failed` = the figures were disproved;
 * `unverified` = they could not be checked. In both cases the backend returns status 'error' with the figures withheld.
 */
export interface ExposureVerification {
  status: 'verified' | 'failed' | 'unverified';
  verified: boolean;
  method: string;
  tolerance_pp: number;
  checks_run: number;
  mismatch_count: number;
  error: string | null;
}

export interface ExposureResult {
  status: 'success' | 'no_equity_holdings' | 'error';
  tool: 'exposure_calculator';
  portfolio_id: number | null;
  portfolio_name: string;
  exposure_basis: string;
  asset_filter: string;
  exposures: ExposureItem[];
  total_equity_weight_pct: number;
  total_holdings_count: number;
  equity_holdings_count: number;
  excluded_bond_holdings: number;
  unmapped_equity_holdings: number;
  error: string | null;
  /** Absent for results that were already errors, and when verification is disabled. */
  verification?: ExposureVerification;
  answer_verification?: AnswerVerification;
}

/**
 * Conversational reply (greeting, thanks, goodbye, identity, capabilities, or a request for clarification).
 * Produced by PortfolioAgent without invoking any tool or Gemini. `details` lists implemented features
 * (capabilities only). No similarity scores are ever included.
 */
export type ConversationIntent = 'greeting' | 'thanks' | 'goodbye' | 'identity' | 'capabilities' | 'clarification';

export interface ConversationResult {
  tool: 'conversation';
  intent: ConversationIntent;
  message: string;
  details?: string[];
  suggested_questions: string[];
}

/**
 * Hybrid analysis: one question answered by the SQL tool AND the independently verified exposure calculation.
 * `complete` is true only when both parts succeeded; `partial` means exactly one did. A component whose figures
 * failed (or could not complete) verification has already had them cleared by the backend.
 */
export interface HybridResult {
  tool: 'hybrid_analysis';
  status: 'success' | 'partial' | 'error';
  complete: boolean;
  portfolio: { portfolio_id: number; portfolio_name: string } | null;
  component_status: { sql: string; exposure: string };
  components: { sql?: SqlResult & { question?: string }; exposure?: ExposureResult };
  issues: string[];
  summary: string;
  error: string | null;
  answer_verification?: AnswerVerification;
}

/**
 * Hypothetical scenario / stress test (backend: portfolio_agent/scenario_tool.py). Never a forecast and never actual
 * performance. Failed or unverified results arrive with status 'error' and the figures withheld.
 */
export interface ScenarioResult {
  tool: 'scenario_analysis';
  status: 'success' | 'no_equity_holdings' | 'error';
  error_type?: string;
  portfolio_id: number | null;
  portfolio_name: string | null;
  scenario: {
    id: string; name: string; description: string; type: 'predefined' | 'custom';
    market_shock_pct: number; sector_shocks: Record<string, number>; is_hypothetical: boolean; is_forecast: boolean;
  } | null;
  starting_exposure: { sector: string; weight_fraction: number; exposure_pct: number; holding_count: number }[];
  sector_impacts: { sector: string; exposure_pct: number; shock_pct: number; contribution_pp: number; shocked: boolean }[];
  unheld_sector_shocks: { sector: string; shock_pct: number }[];
  estimated_portfolio_impact_pct: number | null;
  impact_basis?: string;
  estimated_value_impact: { amount: number; currency: string; aum: number; basis: string } | null;
  value_impact_note?: string | null;
  coverage: {
    equity_exposure_pct: number; shocked_weight_pct: number; unshocked_mapped_weight_pct: number; equity_holdings_count: number;
    unmapped_equity_holdings: number; unmapped_equity_weight_pct: number; excluded_bond_holdings: number; excluded_bond_weight_pct: number;
    recorded_weight_accounted_pct: number; recorded_weight_unaccounted_pct: number; renormalised: boolean;
  } | null;
  assumptions: string[];
  disclaimer: string;
  error: string | null;
  verification?: ExposureVerification;
  /** Final-answer consistency check (portfolio_agent/answer_verifier.py); present on answers from the agent. */
  answer_verification?: AnswerVerification;
}

export interface ScenarioOptions {
  scenarios: { id: string; name: string; description: string; market_shock_pct: number; sector_shocks: Record<string, number> }[];
  sectors: string[];
  shock_limits_pct: { min: number; max: number };
}

export interface AgentResponse {
  status: AgentStatus;
  question: string;
  selected_tool: ToolName | null;
  route_reason: string;
  result: SqlResult | ExposureResult | ConversationResult | HybridResult | ScenarioResult | null;
  error: string | null;
}

export interface Health {
  status: string;
  database_ready: boolean;
  dataset_rows: number | null;
  llm_enabled: boolean;
  gemini_configured: boolean;
}

export interface PortfolioSummary {
  portfolio_id: number;
  portfolio_name: string;
  strategy_type: string;
  target_risk_level: string;
  status: string;
  total_aum: number;
}

export interface Holding {
  symbol: string;
  company_name: string;
  asset_type: 'Stock' | 'Bond' | string;
  sector: string | null;
  quantity: number;
  cost_basis: number;
  current_price: number;
  current_weight: number;
}

export interface PerformanceRecord {
  performance_date: string;
  nav: number;
  total_return_1m: number | null;
  total_return_3m: number | null;
  total_return_6m: number | null;
  total_return_1y: number | null;
  volatility: number | null;
  sharpe_ratio: number | null;
  max_drawdown: number | null;
}

export interface RiskRecord {
  calculation_date: string;
  var_95: number | null;
  var_99: number | null;
  cvar_95: number | null;
  beta: number | null;
  correlation_sp500: number | null;
  tracking_error: number | null;
  information_ratio: number | null;
  sortino_ratio: number | null;
}

export interface PortfolioDetail {
  portfolio: PortfolioSummary;
  holdings: Holding[];
  performance: PerformanceRecord[];
  risk: RiskRecord[];
}

export interface EvalQuestionResult {
  type: string;
  expected_tool: string;
  selected_tool: string;
  generation: string;
  rows: string;
  result: 'PASS' | 'FAIL';
}

export interface ReportMeta {
  source: string;
  mode: string | null;
  score: number | null;
  total: number | null;
  model: string | null;
  started: string | null;
  /** 'final' = the preferred final live run; 'latest' = a stand-in when no final report exists; 'historical' = an earlier, superseded live run. */
  role?: 'final' | 'latest' | 'historical' | null;
}

export interface EvalQuestion {
  id: number;
  question: string;
  type: string;
  deterministic: EvalQuestionResult | null;
  live: EvalQuestionResult | null;
  live_failures: string[];
  historical_live?: EvalQuestionResult | null;
  historical_live_failures?: string[];
  targeted: EvalQuestionResult | null;
}

export interface Evaluation {
  deterministic: ReportMeta | null;
  live: ReportMeta | null;
  historical_live?: ReportMeta | null;
  live_subset: ReportMeta | null;
  live_generation: { sql_questions: number; by_gemini: number; fallbacks: number } | null;
  questions: EvalQuestion[];
}
