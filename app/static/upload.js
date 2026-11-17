// 大文件分片上传：每片单独上传、失败自动重试；中断后重新选择同一个文件，从没传完的分片继续。
// 面向国内到新加坡的不稳定网络：每个请求有超时，网络类错误一直重试（最长等 60 秒再试），
// 只有明确的业务错误（没权限、上传已取消等）才停下来。
(function () {
  const CONCURRENCY = 3;
  const PART_TIMEOUT_MS = 5 * 60 * 1000;   // 一片 8 MB，5 分钟没传完就当连接卡死，重试
  const API_TIMEOUT_MS = 60 * 1000;
  const GIVE_UP_MS = 6 * 60 * 60 * 1000;   // 同一片连续失败 6 小时才放弃
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const fmt = b => b > 1073741824 ? (b / 1073741824).toFixed(2) + ' GB' : (b / 1048576).toFixed(1) + ' MB';
  const retriable = e => !e.status || e.status >= 500 || e.status === 408 || e.status === 429;

  async function request(method, url, {body, json, headers, timeout, signal} = {}) {
    const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), timeout || API_TIMEOUT_MS);
    const onStop = () => ctl.abort();
    if (signal) signal.addEventListener('abort', onStop, {once: true});
    try {
      const r = await fetch(url, {method, credentials: 'same-origin', signal: ctl.signal,
        headers: json ? {'Content-Type': 'application/json'} : (headers || {}),
        body: json ? JSON.stringify(json) : body});
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw Object.assign(new Error(j.error || ('HTTP ' + r.status)), {status: r.status});
      return j;
    } catch (e) {
      if (signal && signal.aborted) throw Object.assign(new Error('已停止'), {stopped: true});
      throw e;  // 超时 / 断网：没有 status，按网络错误重试
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onStop);
    }
  }

  // 网络类错误一直重试；onWait 用来在页面上显示“网络不稳定，xx 秒后重试”
  async function withRetry(fn, signal, onWait) {
    const t0 = Date.now();
    for (let attempt = 0; ; attempt++) {
      if (signal.aborted) throw Object.assign(new Error('已停止'), {stopped: true});
      try { return await fn(); } catch (e) {
        if (e.stopped || !retriable(e) || Date.now() - t0 > GIVE_UP_MS) throw e;
        const wait = Math.min(60000, 1000 * 2 ** Math.min(attempt, 6));
        onWait && onWait(wait, e);
        if (!navigator.onLine) await new Promise(r => window.addEventListener('online', r, {once: true}));
        else await sleep(wait);
      }
    }
  }

  function setup(box) {
    const input = box.querySelector('input[type=file]'), status = box.querySelector('.up-status'),
          bar = box.querySelector('.up-bar'), fill = bar.firstElementChild;
    const say = (t, err) => { status.textContent = t; status.classList.toggle('err', !!err); };
    let busy = false;

    async function start(file) {
      if (busy) return;
      busy = true;
      const ds = box.dataset, key = ['up', ds.episode, ds.stage, ds.field, file.name, file.size, file.lastModified].join(':');
      input.disabled = true; bar.hidden = false;
      const stop = new AbortController();
      const leave = e => { e.preventDefault(); e.returnValue = ''; };
      window.addEventListener('beforeunload', leave);
      let resume = null; try { resume = localStorage.getItem(key); } catch (e) {}
      let note = '';
      try {
        const up = await withRetry(() => request('POST', '/api/uploads', {json: {episode_id: ds.episode, stage_key: ds.stage,
          field: ds.field, filename: file.name, size: file.size, resume_id: resume}}), stop.signal,
          w => say(`连接服务器失败，${Math.round(w / 1000)} 秒后重试…`));
        try { localStorage.setItem(key, up.id); } catch (e) {}
        if (up.status !== 'done') {
          const got = new Set(up.received), todo = [];
          for (let n = 0; n < up.parts_total; n++) if (!got.has(n)) todo.push(n);
          let done = Math.min(got.size * up.part_size, file.size);
          const t0 = Date.now(), done0 = done;
          const tick = () => {
            const pct = file.size ? done / file.size * 100 : 100, speed = (done - done0) / Math.max(1, (Date.now() - t0) / 1000);
            fill.style.width = pct.toFixed(1) + '%';
            say(`${got.size ? '续传 · ' : ''}${fmt(done)} / ${fmt(file.size)}（${pct.toFixed(1)}%）· ${fmt(speed)}/s${note} · 请不要关闭页面`);
          };
          tick();
          const putPart = async n => {
            const start = n * up.part_size, blob = file.slice(start, Math.min(start + up.part_size, file.size));
            await withRetry(async () => {
              const t = await request('GET', `/api/uploads/${up.id}/parts/${n}/target`, {signal: stop.signal});
              await request(t.method, t.url, {body: blob, headers: t.headers, timeout: PART_TIMEOUT_MS, signal: stop.signal});
            }, stop.signal, w => { note = ` · 网络不稳定，${Math.round(w / 1000)} 秒后重试`; tick(); });
            note = '';
            return blob.size;
          };
          await Promise.all(Array.from({length: CONCURRENCY}, async () => {
            while (todo.length && !stop.signal.aborted) {
              const n = todo.shift();
              try { done += await putPart(n); tick(); } catch (e) { stop.abort(); throw e; }  // 一个失败，全部停下
            }
          }));
          say('合并中…');
          await withRetry(() => request('POST', `/api/uploads/${up.id}/complete`, {timeout: 5 * 60 * 1000}), stop.signal,
            w => say(`合并请求失败，${Math.round(w / 1000)} 秒后重试…`));
        }
        try { localStorage.removeItem(key); } catch (e) {}
        fill.style.width = '100%'; say('上传完成，正在刷新…');
        window.removeEventListener('beforeunload', leave);
        setTimeout(() => location.reload(), 600);
      } catch (e) {
        stop.abort();
        window.removeEventListener('beforeunload', leave);
        say('上传中断：' + e.message + '。重新选择同一个文件会从断点继续。', true);
        input.disabled = false; input.value = ''; busy = false;
      }
    }

    input.addEventListener('change', () => input.files[0] && start(input.files[0]));
    box.addEventListener('dragover', e => { e.preventDefault(); box.classList.add('drag'); });
    box.addEventListener('dragleave', () => box.classList.remove('drag'));
    box.addEventListener('drop', e => { e.preventDefault(); box.classList.remove('drag');
      if (e.dataTransfer.files[0]) start(e.dataTransfer.files[0]); });
  }
  document.querySelectorAll('.uploader').forEach(setup);
})();
