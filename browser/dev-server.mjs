import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

const assets = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ['/index.html', ['index.html', 'text/html; charset=utf-8']],
  ...['demo.mjs', 'conversation.mjs', 'livekit-adapter.mjs'].map(name =>
    ['/' + name, [name, 'text/javascript; charset=utf-8']]),
  ['/vendor/livekit-client.esm.mjs',
    ['node_modules/livekit-client/dist/livekit-client.esm.mjs', 'text/javascript; charset=utf-8']],
]);
export function createDevServer() {
  return createServer(async (request, response) => {
    const host = request.headers.host;
    if (!host || !/^127\.0\.0\.1(:\d+)?$/.test(host)) {
      response.writeHead(403).end(); return;
    }
    const asset = assets.get(request.url);
    if (request.method !== 'GET' || !asset) { response.writeHead(404).end(); return; }
    try {
      const data = await readFile(new URL(asset[0], import.meta.url));
      response.writeHead(200, { 'Content-Type': asset[1], 'Cache-Control': 'no-store',
        'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff' }).end(data);
    } catch { response.writeHead(500).end('配信資産を読み込めません'); }
  });
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const server = createDevServer();
  server.listen(4173, '127.0.0.1', () => process.stdout.write('http://127.0.0.1:4173/\n'));
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => {
    server.closeAllConnections(); server.close();
  });
}
