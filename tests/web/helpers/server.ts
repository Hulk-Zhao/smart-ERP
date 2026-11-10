import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import type { AddressInfo } from 'node:net';

export interface CapturedRequest {
  method: string;
  path: string;
  query: Record<string, string[]>;
  headers: Record<string, string | string[] | undefined>;
  body: unknown;
  rawBody: string;
}

export interface Reply {
  status?: number;
  body?: unknown;
  text?: string;
  headers?: Record<string, string>;
}

export type RouteHandler = (request: CapturedRequest) => Reply | Promise<Reply>;

export interface TestApi {
  baseUrl: string;
  requests: CapturedRequest[];
  close: () => Promise<void>;
}

function parseQuery(search: string): Record<string, string[]> {
  const params = new URLSearchParams(search);
  const result: Record<string, string[]> = {};
  for (const [key, value] of params.entries()) {
    (result[key] ??= []).push(value);
  }
  return result;
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on('data', (chunk: Buffer) => chunks.push(chunk));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

/**
 * 真实 HTTP 测试服务器：前端 client 走真实 fetch，只把后端替换成此 server。
 * 这样能验证 scope、X-Session-Token、Idempotency-Key 与 problem+json 的真实链路。
 */
export async function startApi(handler: RouteHandler): Promise<TestApi> {
  const requests: CapturedRequest[] = [];
  const server = createServer(async (req: IncomingMessage, res: ServerResponse) => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
    const rawBody = await readBody(req);
    let body: unknown = undefined;
    if (rawBody) {
      try {
        body = JSON.parse(rawBody);
      } catch {
        body = rawBody;
      }
    }
    const captured: CapturedRequest = {
      method: req.method ?? 'GET',
      path: url.pathname,
      query: parseQuery(url.search),
      headers: req.headers,
      body,
      rawBody,
    };
    requests.push(captured);

    try {
      const reply = await handler(captured);
      const status = reply.status ?? 200;
      const headers: Record<string, string> = { ...(reply.headers ?? {}) };
      if (reply.text !== undefined) {
        headers['content-type'] ??= 'text/csv; charset=utf-8';
        res.writeHead(status, headers);
        res.end(reply.text);
      } else {
        headers['content-type'] ??= 'application/json';
        res.writeHead(status, headers);
        res.end(JSON.stringify(reply.body ?? null));
      }
    } catch (error) {
      res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ status: 500, detail: (error as Error).message }));
    }
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address() as AddressInfo;

  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    requests,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
      }),
  };
}

export function problem(status: number, code: string, detail: string, extra: Record<string, unknown> = {}): Reply {
  return {
    status,
    headers: { 'content-type': 'application/problem+json' },
    body: {
      type: 'about:blank',
      title: status === 403 ? 'Forbidden' : status === 409 ? 'Conflict' : 'Error',
      status,
      detail,
      code,
      trace_id: `trace-${code}`,
      retriable: false,
      ...extra,
    },
  };
}
