import { describe, expect, it, vi } from 'vitest';
import { ApiError, api } from './client';

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

  it('explains how to start the local server when it is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
    const err = await api.health().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toContain('python -m portfolio_api');
  });
});
