import type { AgentResponse, Evaluation, ExposureResult, Health, PortfolioDetail, PortfolioSummary, ScenarioOptions, ScenarioResult, SessionState } from './types';

/** Error with a user-presentable message. Never contains stack traces or server internals. */
export class ApiError extends Error {
  readonly status: number | null;
  constructor(message: string, status: number | null = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export const SERVER_HINT = 'Start the local server with:  python -m portfolio_api';

export const SESSION_EXPIRED = 'Your session has expired. Please enter the access code again.';

type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();

/**
 * Subscribe to "the server refused a data request because there is no valid session" (never fired for the login call
 * itself). The access gate uses it to return to the access screen when a session expires. Returns an unsubscribe function.
 */
export function onUnauthorized(listener: Listener): () => void {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: 'same-origin', // the signed session cookie travels with same-origin requests only; JavaScript never sees it
      headers: { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}) },
    });
  } catch {
    throw new ApiError(`The local server is not reachable. ${SERVER_HINT}`);
  }
  if (!response.ok) {
    let detail = '';
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
    } catch {
      /* non-JSON error body: use the generic message below */
    }
    if (response.status === 401 && !path.startsWith('/api/auth/')) {
      unauthorizedListeners.forEach((listener) => listener());
      throw new ApiError(SESSION_EXPIRED, 401);
    }
    throw new ApiError(detail || `The server returned an error (HTTP ${response.status}).`, response.status);
  }
  return (await response.json()) as T;
}

export const api = {
  session: () => request<SessionState>('/api/auth/session'),
  login: (code: string) => request<{ authenticated: boolean }>('/api/auth/login', { method: 'POST', body: JSON.stringify({ code }) }),
  logout: () => request<{ authenticated: boolean }>('/api/auth/logout', { method: 'POST' }),
  health: () => request<Health>('/api/health'),
  ask: (question: string) =>
    request<AgentResponse>('/api/ask', { method: 'POST', body: JSON.stringify({ question }) }),
  exposure: (portfolioName: string) =>
    request<ExposureResult>('/api/exposure', { method: 'POST', body: JSON.stringify({ portfolio_name: portfolioName }) }),
  scenarioOptions: () => request<ScenarioOptions>('/api/scenarios'),
  scenario: (body: { portfolio_name: string; scenario_id?: string; market_shock_pct?: number; sector_shocks?: Record<string, number> }) =>
    request<ScenarioResult>('/api/scenario', { method: 'POST', body: JSON.stringify(body) }),
  portfolios: () => request<PortfolioSummary[]>('/api/portfolios'),
  portfolio: (id: number) => request<PortfolioDetail>(`/api/portfolios/${id}`),
  evaluation: () => request<Evaluation>('/api/evaluation'),
};
