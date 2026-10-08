import assert from 'node:assert/strict';
import { once } from 'node:events';
import { test } from 'node:test';

test('ローカル配信入口からHTMLとそのscriptと固定SDK資産を取得できる', { timeout: 10000 }, async t => {
  const { createDevServer } = await import('../dev-server.mjs');
  const server = createDevServer();
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const origin = `http://127.0.0.1:${server.address().port}`;
  const response = await fetch(origin);
  assert.equal(response.status, 200);
  assert.match(response.headers.get('content-type'), /text\/html/);
  const html = await response.text();
  const scripts = [...html.matchAll(/<script\b[^>]*\bsrc=["']([^"']+)["'][^>]*>/gi)]
    .map(match => match[1]);
  assert.ok(scripts.length > 0, '利用者のHTML入口から実行moduleへ到達できる');
  for (const script of scripts) {
    const asset = await fetch(new URL(script, origin));
    assert.equal(asset.status, 200);
    assert.match(asset.headers.get('content-type'), /javascript/);
  }
  const sdk = await fetch(`${origin}/vendor/livekit-client.esm.mjs`);
  assert.equal(sdk.status, 200);
  assert.match(sdk.headers.get('content-type'), /javascript/);
  assert.ok((await sdk.text()).length > 0);
});
