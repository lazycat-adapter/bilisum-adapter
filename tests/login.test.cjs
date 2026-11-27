const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../content/login-compat/session.js'), 'utf8');

async function simulate({ authenticated = false, auto = 200, remember = false, status = 200, canAutoBind = false } = {}) {
  const calls = [], links = [], notices = [], data = new Map();
  if (remember) data.set('lazycat-login:/session', 'pending');
  const win = { location: { origin: 'https://app.test', href: 'https://app.test/', reload() { calls.push('reload'); } } };
  win.top = win;
  const node = () => ({ style: {}, append(...items) { for (const i of items) if (i.href) links.push(i.href); }, setAttribute() {} });
  const sandbox = {
    window: win, location: win.location, URL, console,
    document: { readyState: 'complete', body: { append(n) { notices.push(n); } }, createElement: node },
    sessionStorage: { getItem: k => data.get(k), setItem: (k,v) => data.set(k,v), removeItem: k => data.delete(k) },
    ctx: { params: { statusPath:'/status', sessionPath:'/session', oidcPath:'/old-login' } },
    fetch: async (url, options) => {
      calls.push([String(url), options.method || 'GET']);
      const code = String(url).endsWith('/status') ? status : auto;
      return {status:code, ok:code >= 200 && code < 300, json:async () => ({authenticated,canAutoBind,oidcEnabled:true})};
    },
  };
  await vm.runInNewContext(script, sandbox);
  return { calls, links, data, notices };
}

test('existing login opens directly', async () => {
  const r = await simulate({authenticated:true}); assert.deepEqual(r.calls, [['/status','GET']]);
});
test('first open establishes one session then reloads', async () => {
  const r = await simulate(); assert.deepEqual(r.calls, [['/status','GET'],['/session','POST'],'reload']);
});
test('expired legacy login offers explicit OIDC and leaves native input accessible', async () => {
  const r = await simulate({auto:409}); assert.deepEqual(r.links, ['/old-login']); assert.equal(r.calls.length, 2);
});
test('missing cookie after reload does not loop; server error leaves a fallback', async () => {
  assert.equal((await simulate({remember:true})).calls.length, 1);
  assert.equal((await simulate({auto:503})).notices.length, 1);
  assert.equal((await simulate({status:500})).calls.length, 1);
});

test('a proven matching old identity can link without interrupting the active session', async () => {
  const r = await simulate({authenticated:true,canAutoBind:true});
  assert.deepEqual(r.calls, [['/status','GET'],['/session','POST']]);
  assert.equal(r.notices.length,0);
});
