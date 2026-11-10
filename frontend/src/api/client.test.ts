import { describe, expect, it, vi } from 'vitest';
import { ApiError, SESSION_EXPIRED, api, onUnauthorized } from './client';

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('api client', () => {
  it('posts the question as JSON and returns the response verbatim', async () => {
    const fetchMock = vi.fn(async () => json({ status: 'success', question: 'q', selected_tool: 'sql_query', route_reason: 'r', result: null, error: null }));
    vi.stubGlobal('fetch', fetchMock);
    const response = await api.ask('How many portfolios?');
    expect(response.status).toBe('success');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/ask');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ question: 'How many portfolios?' });
  });

  it('uses only relative /api URLs and sends no credentials or keys', async () => {
    const fetchMock = vi.fn(async () => json([]));
    vi.stubGlobal('fetch', fetchMock);
    await api.portfolios();
    await api.evaluation();
    for (const call of fetchMock.mock.calls as unknown as [string, RequestInit][]) {
      expect(call[0].startsWith('/api/')).toBe(true);
      expect(JSON.stringify(call[1] ?? {}).toLowerCase()).not.toMatch(/key|token|authorization/);
    }
  });

  it('surfaces the server detail message for HTTP errors, without internals', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'Portfolio not found.' }, 404)));
    await expect(api.portfolio(9)).rejects.toMatchObject({ name: 'ApiError', status: 404, message: 'Portfolio not found.' });
  });

  it('falls back to a generic message for non-JSON errors', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>boom</html>', { status: 502 })));
    await expect(api.health()).rejects.toThrow('The server returned an error (HTTP 502).');
  });

  it('sends the session cookie to the same origin only and never puts a secret in a header or URL', async () => {
    const fetchMock = vi.fn(async () => json({}));
    vi.stubGlobal('fetch', fetchMock);
    await api.portfolios();
    await api.login('some-access-code-1234');
    for (const call of fetchMock.mock.calls as unknown as [string, RequestInit][]) {
      expect(call[1].credentials).toBe('same-origin');
      expect(call[0]).not.toContain('some-access-code-1234');
      expect(JSON.stringify(call[1].headers ?? {})).not.toContain('some-access-code-1234');
    }
  });

  it('talks to the three access endpoints with the right methods', async () => {
    const fetchMock = vi.fn(async () => json({ authenticated: true, auth_required: true }));
    vi.stubGlobal('fetch', fetchMock);
    await api.session();
    await api.login('some-access-code-1234');
    await api.logout();
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map(([url, init]) => `${init?.method ?? 'GET'} ${url}`)).toEqual(['GET /api/auth/session', 'POST /api/auth/login', 'POST /api/auth/logout']);
    expect(JSON.parse(calls[1][1].body as string)).toEqual({ code: 'some-access-code-1234' });
  });

  it('tells listeners when a data request is refused for lack of a session, with a plain message', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'Authentication required.' }, 401)));
    const listener = vi.fn();
    const off = onUnauthorized(listener);
    await expect(api.portfolios()).rejects.toMatchObject({ status: 401, message: SESSION_EXPIRED });
    expect(listener).toHaveBeenCalledTimes(1);
    off();
    await expect(api.portfolios()).rejects.toBeInstanceOf(ApiError);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('does not treat a wrong access code as an expired session', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'Invalid access code.' }, 401)));
    const listener = vi.fn();
    const off = onUnauthorized(listener);
    await expect(api.login('wrong')).rejects.toMatchObject({ status: 401, message: 'Invalid access code.' });
    expect(listener).not.toHaveBeenCalled();
    off();
  });

  it('explains how to start the local server when it is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
    const err = await api.health().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toContain('python -m portfolio_api');
  });
});
