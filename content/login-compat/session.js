/* LazyCat browser inject. The application adapter supplies three same-origin paths.
   Authentication credentials stay between the ingress and the application server. */
(async () => {
  if (window.top !== window || window.__lazycatSessionStarted) return;
  window.__lazycatSessionStarted = true;
  const { statusPath, sessionPath, oidcPath, providerName = '懒猫账号' } = ctx.params;
  for (const value of [statusPath, sessionPath, oidcPath]) {
    if (typeof value !== 'string' || !value.startsWith('/') ||
        new URL(value, location.href).origin !== location.origin) return;
  }
  function openSession() {
    return fetch(sessionPath, {...options, method:'POST', headers:{'X-Requested-With':'LazyCat-Login'}});
  }
  const marker = 'lazycat-login:' + sessionPath;
  const options = {credentials:'same-origin', cache:'no-store', redirect:'error'};
  async function notice(message) {
    if (document.readyState === 'loading') {
      await new Promise(resolve => document.addEventListener('DOMContentLoaded', resolve, {once:true}));
    }
    const panel = document.createElement('aside');
    panel.setAttribute('role','status');
    panel.style.cssText = 'position:fixed;bottom:1rem;right:1rem;z-index:9999;background:#fff;color:#171717;padding:1rem;max-width:25rem;border:1px solid #888;border-radius:.5rem';
    const link = document.createElement('a');
    link.href = oidcPath;
    link.textContent = '使用' + providerName + '登录';
    panel.append(message + ' ', link);
    document.body.append(panel);
  }
  try {
    const status = await fetch(statusPath, options);
    if (!status.ok) throw new Error('status');
    const current = await status.json();
    if (current.authenticated === true) {
      // The server alone decides whether a verified old identity can be linked.
      if (current.canAutoBind === true) await openSession().catch(() => {});
      sessionStorage.removeItem(marker);
      return;
    }
    if (sessionStorage.getItem(marker)) {
      sessionStorage.removeItem(marker);
      return notice('未能保留登录状态，请手动登录。');
    }
    const result = await openSession();
    if (result.status === 409) return notice('请确认原账号，以继续访问已有数据。');
    if (!result.ok) throw new Error('session');
    sessionStorage.setItem(marker, 'pending');
    location.reload();
  } catch {
    return notice('自动登录暂不可用，可使用原有登录入口。');
  }
})();
