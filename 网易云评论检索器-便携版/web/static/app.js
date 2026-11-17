/* 网易云评论检索器 —— 前端逻辑（纯 vanilla JS，无框架、无 CDN、无 AI） */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  items: [],          // 解析出来的候选曲目
  selected: new Set(),
  jobId: null,
  timer: null,
  snap: null,         // 最近一次任务快照
};

/* ------------------------------------------------------------ 小工具 */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

const fmt = (n) => Number(n || 0).toLocaleString('zh-CN');

const SRC_LABEL = { id: '歌曲ID', search: '搜索', playlist: '歌单', album: '专辑' };

/* ------------------------------------------------------- 后端地址自发现
 * 页面可能被三种方式打开：
 *   1. 后端自己托管      http://127.0.0.1:8765/          —— 同源，最省事
 *   2. 别的预览服务器    http://127.0.0.1:51156/...      —— 跨源，需后端放行 CORS
 *   3. 直接双击文件      file:///.../index.html          —— 跨源且 Origin 为 null
 * 后两种下相对路径 /api/... 会打空，所以这里挨个试探候选地址，
 * 找到活的后端就固定用它，用户不需要关心自己是从哪个入口进来的。
 */
let apiBase = null;        // null = 尚未探测成功；'' = 同源；其它 = 绝对地址
let baseResolved = false;  // 只有探测成功才置 true；失败保持 false，下次才能重试

// 后端失联时的统一话术：直接告诉用户「该做什么」，而不是只报一个 HTTP 错误码
const BACKEND_DOWN =
  '连不上本地后端服务。这个页面本身不会联网，必须有一个后端进程在跑：' +
  '请回到那个启动终端窗口（「启动界面.command」/「启动界面.bat」打开的窗口），' +
  '确认它还开着；如果已经关掉，请重新双击启动，然后刷新本页。';

function candidates() {
  const list = [''];
  const sameAsBackend = location.protocol.startsWith('http') && location.port === '8765';
  if (!sameAsBackend) {
    for (let port = 8765; port <= 8775; port += 1) list.push(`http://127.0.0.1:${port}`);
  }
  return list;
}

/*
 * 超时分两档，绝不能共用：
 *  - SHORT_MS：本地接口（健康检查、任务轮询）。后端在本机，毫秒级就该回来，
 *    2.5 秒还没响应基本可以判定「进程没了」。
 *  - LONG_MS ：需要实时联网等网易云答复的接口（如 /api/resolve）。
 *    这类请求正常也要 5~20 秒，网络抖动时会到 30 秒以上。
 *    早先两者都用 2.5 秒，于是「解析歌单」必然被前端自己掐断，
 *    还误报成「后端服务可能已经停止」——后端其实一直在跑。
 */
const SHORT_MS = 2500;
const LONG_MS = 180000;

function fetchWithTimeout(url, opts = {}, ms = SHORT_MS) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  return fetch(url, { ...opts, signal: ctl.signal }).finally(() => clearTimeout(timer));
}

function resetBase() {
  apiBase = null;
  baseResolved = false;
}

async function ensureBase() {
  if (baseResolved) return apiBase;
  for (const base of candidates()) {
    try {
      const res = await fetchWithTimeout(`${base}/api/health`);
      if (res.ok) {
        apiBase = base;
        baseResolved = true;
        return apiBase;
      }
    } catch (e) { /* 试下一个 */ }
  }
  // 注意：这里**不能**把 apiBase 写成 ''。一旦写死，baseResolved 之外的分支
  // 就再也不会重新探测，用户重启服务后页面仍然连不上，必须手动刷新。
  resetBase();
  throw new Error(BACKEND_DOWN);
}

function apiUrl(path) {
  return (apiBase || '') + path;
}

// 把网络层异常翻译成「用户照着做就能解决」的提示
// label 非空表示这是一次长耗时联网请求，超时话术必须与「后端挂了」分开说
function friendly(e, label = '') {
  if (e && typeof e.message === 'string' && e.message === BACKEND_DOWN) return e;
  if (e && (e.name === 'AbortError' || e.name === 'TimeoutError')) {
    if (label) {
      return new Error(
        `${label}等待超过 ${Math.round(LONG_MS / 1000)} 秒仍未返回，已放弃本次请求。` +
        '这通常是网易云接口临时变慢或本机网络波动引起的，' +
        '并不代表后端服务已停止（启动窗口可以继续留着）。请稍等片刻再点一次。');
    }
    return new Error('请求超时，后端服务可能已经停止。请确认启动窗口还开着，再刷新页面重试。');
  }
  if (e instanceof TypeError) return new Error(BACKEND_DOWN);  // fetch 网络层失败
  return e;
}

async function post(path, body, opts = {}) {
  const ms = opts.ms || SHORT_MS;
  const label = opts.label || '';
  let base;
  try { base = await ensureBase(); } catch (e) { throw friendly(e); }
  let res;
  try {
    res = await fetchWithTimeout(base + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }, ms);
  } catch (e) {
    resetBase();          // 服务可能中途挂了，下次请求重新探测
    throw friendly(e, label);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function get(path, opts = {}) {
  const ms = opts.ms || SHORT_MS;
  let base;
  try { base = await ensureBase(); } catch (e) { throw friendly(e); }
  let res;
  try {
    res = await fetchWithTimeout(base + path, {}, ms);
  } catch (e) {
    resetBase();
    throw friendly(e, opts.label || '');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* ------------------------------------------------------------ 健康检查 */
const OK_TAG = '<span class="dot"></span>';
const BAD_TAG = '<span class="dot" style="background:#ff6b6b;box-shadow:0 0 8px #ff6b6b"></span>';

async function checkHealth(verbose = false) {
  const tag = $('#healthTag');
  resetBase(); // 每次检查都重新探测，避免后端换端口后失联
  try {
    await ensureBase();
    const shown = apiBase || location.origin;
    tag.innerHTML = `${OK_TAG}已连接后端 ${shown} · 不调用 AI`;
    return true;
  } catch (e) {
    tag.innerHTML = `${BAD_TAG}未连接后端服务`;
    if (verbose) {
      const note = $('#resolveNote');
      note.className = 'inline-note error';
      note.textContent = BACKEND_DOWN;
    }
    return false;
  }
}

/* ------------------------------------------------------------ 第一步 */
async function resolveSongs() {
  const query = $('#query').value.trim();
  const note = $('#resolveNote');
  const btn = $('#btnResolve');

  if (!query) {
    note.className = 'inline-note error';
    note.textContent = '请先输入歌曲名、歌曲 ID 或链接';
    return;
  }

  btn.disabled = true;
  btn.textContent = '解析中…';
  note.className = 'inline-note';
  note.textContent = '正在联网查询网易云…（通常 5~20 秒，网络慢时会久一些，请勿关闭启动窗口）';
  $('#cardSongs').hidden = true;

  try {
    // 这一步要实时等网易云答复，必须用长超时，否则会被前端自己掐断
    const data = await post('/api/resolve', { query }, { ms: LONG_MS, label: '解析曲目' });
    state.items = data.items || [];
    state.selected = new Set(state.items.map((i) => i.song_id));
    renderSongs();

    const notes = (data.notes || []).filter(Boolean);
    if (state.items.length) {
      note.className = 'inline-note ok';
      let msg = (notes.join('；') || `解析到 ${state.items.length} 首`) +
        ' —— 已默认全选，可在下方取消勾选';
      if (state.items.length > 20) {
        msg += `。曲目较多（${state.items.length} 首），首次使用建议先只勾选 1~2 首试跑，` +
          '确认流程通了，再回来全选。';
      }
      note.textContent = msg;
      $('#cardSongs').hidden = false;
    } else {
      note.className = 'inline-note error';
      note.textContent = notes.join('；') || '没有解析到任何曲目';
    }
  } catch (e) {
    note.className = 'inline-note error';
    note.textContent = '解析失败：' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = '解析曲目';
  }
}

function renderSongs() {
  const ul = $('#songList');
  $('#songCount').textContent = `已选 ${state.selected.size} / ${state.items.length} 首`;

  ul.innerHTML = state.items.map((it) => `
    <li class="song-item" data-id="${it.song_id}">
      <input type="checkbox" ${state.selected.has(it.song_id) ? 'checked' : ''}>
      <span class="name">${esc(it.name || '(未命名)')}</span>
      <span class="meta">${esc(it.artists || '')}${it.album ? ' · ' + esc(it.album) : ''}</span>
      <span class="src">${esc(SRC_LABEL[it.source] || it.source || '')}</span>
      <span class="sid">${it.song_id}</span>
    </li>`).join('');

  $$('.song-item', ul).forEach((li) => {
    li.addEventListener('click', () => {
      const id = Number(li.dataset.id);
      if (state.selected.has(id)) state.selected.delete(id);
      else state.selected.add(id);
      $('input', li).checked = state.selected.has(id);
      $('#songCount').textContent = `已选 ${state.selected.size} / ${state.items.length} 首`;
    });
  });
}

/* ------------------------------------------------------------ 第二步 */
async function startScan() {
  const note = $('#startNote');
  const songs = state.items.filter((i) => state.selected.has(i.song_id));
  const uid = $('#uid').value.trim();
  const nickname = $('#nickname').value.trim();

  if (!songs.length) {
    note.className = 'inline-note error';
    note.textContent = '请至少勾选一首曲目';
    return;
  }
  if (!uid && !nickname) {
    note.className = 'inline-note error';
    note.textContent = '请填写目标用户的 uid 或昵称，否则无从判定命中';
    return;
  }

  $('#btnStart').disabled = true;
  note.className = 'inline-note';
  note.textContent = '正在创建任务…';

  try {
    const data = await post('/api/scan', {
      songs,
      uid,
      nickname,
      fuzzy: $('#fuzzy').checked,
      workers: Number($('#workers').value),
      interval: Number($('#interval').value),
      max_pages: Number($('#maxPages').value),
      cookie: $('#cookie').value.trim(),
      stop_on_match: $('#stopOnMatch').checked,
      auto_rescan: $('#autoRescan').checked,
    });

    state.jobId = data.job_id;
    state.snap = null;
    note.className = 'inline-note ok';
    note.textContent = `任务已启动（${data.songs} 首）`;

    $('#cardProgress').hidden = false;
    $('#cardResult').hidden = false;
    $('#cardProgress').scrollIntoView({ behavior: 'smooth', block: 'start' });
    startPolling();
  } catch (e) {
    note.className = 'inline-note error';
    note.textContent = '启动失败：' + e.message;
  } finally {
    $('#btnStart').disabled = false;
  }
}

/* ------------------------------------------------------------ 轮询 */
function startPolling() {
  stopPolling();
  pollOnce();
  state.timer = setInterval(pollOnce, 900);
}

function stopPolling() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
}

async function pollOnce() {
  if (!state.jobId) return;
  try {
    const snap = await get('/api/job?id=' + encodeURIComponent(state.jobId));
    state.snap = snap;
    renderProgress(snap);
    renderResults(snap);
    if (snap.status !== 'running') {
      stopPolling();
      $('#btnStop').disabled = true;
    }
  } catch (e) {
    stopPolling();
    $('#jobStatus').textContent = '连接中断';
    $('#jobStatus').className = 'badge stopped';
  }
}

/* ------------------------------------------------------------ 进度渲染 */
const STATUS_TEXT = { running: '运行中', done: '已完成', stopped: '已停止', error: '出错' };

function renderProgress(snap) {
  const badge = $('#jobStatus');
  badge.textContent = STATUS_TEXT[snap.status] || snap.status;
  badge.className = 'badge ' + (snap.status === 'done' ? 'done'
    : snap.status === 'running' ? '' : 'stopped');
  $('#btnStop').disabled = snap.status !== 'running';

  const cards = [
    ['已扫评论', fmt(snap.scanned), ''],
    ['命中目标', fmt(snap.hits), snap.hits ? 'ok' : ''],
    ['完成曲目', `${snap.songs_done} / ${snap.songs_total}`, ''],
    ['耗时', snap.elapsed + ' s', ''],
    ['速率', fmt(snap.rate) + ' 条/秒', ''],
  ];
  if (snap.songs_truncated) cards.push(['疑似截断', snap.songs_truncated + ' 首', 'accent']);

  $('#statRow').innerHTML = cards.map(([k, v, cls]) =>
    `<div class="stat"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`).join('');

  $('#progressList').innerHTML = snap.songs.map((s) => {
    let pct;
    if (s.status === 'done' || s.status === 'stopped') pct = 100;
    else if (s.total_count) pct = Math.min(99, (s.scanned / s.total_count) * 100);
    else pct = s.status === 'running' ? 12 : 0;

    let tags = '';
    if (s.status === 'running') tags += '<span class="p-tag run">扫描中</span>';
    else if (s.status === 'pending') tags += '<span class="p-tag">排队中</span>';
    else if (s.status === 'error') tags += '<span class="p-tag err">抓取失败</span>';
    else if (s.status === 'stopped') tags += '<span class="p-tag warn">已中断</span>';
    if (s.hits) tags += `<span class="p-tag hit">命中 ${s.hits}</span>`;
    if (s.truncated) tags += '<span class="p-tag warn">疑似被限流截断</span>';
    if (s.rescanned) tags += '<span class="p-tag">已降速重扫</span>';

    const detail = [
      `已扫 ${fmt(s.scanned)}${s.total_count ? ' / ' + fmt(s.total_count) : ''} 条`,
      `${s.pages} 页`,
      s.elapsed ? s.elapsed + 's' : '',
      s.error ? s.error : '',
    ].filter(Boolean).join(' · ');

    return `<li class="p-item ${s.hits ? 'hit' : ''} ${s.status === 'error' ? 'err' : ''}">
      <div class="p-top">
        <span class="p-name">${esc(s.label)}${s.artists ? ' <span class="uid">' + esc(s.artists) + '</span>' : ''}</span>
        ${tags}
        <span class="p-stat">${detail}</span>
      </div>
      <div class="p-bar"><i style="width:${pct.toFixed(1)}%"></i></div>
    </li>`;
  }).join('');
}

/* ------------------------------------------------------------ 结果渲染 */
const MAX_ROWS = 500;

function renderResults(snap) {
  const rows = snap.results || [];
  $('#hitCount').textContent = `${rows.length} 条`;
  $('#cardResult').hidden = false;

  if (!rows.length) {
    $('#hitBody').innerHTML = `<div class="empty">${
      snap.status === 'running'
        ? '正在扫描中，命中记录会实时出现在这里…'
        : '扫描完毕，未找到该用户在这批曲目下的评论'
    }</div>`;
    return;
  }

  const shown = rows.slice(0, MAX_ROWS);
  const body = shown.map((r) => `
    <tr>
      <td class="time">${esc(r.time_str)}</td>
      <td>${esc(r.song_name)}</td>
      <td>
        <div class="nick">${esc(r.nickname)}</div>
        <div class="uid">uid=${r.user_id}</div>
        <div class="reason">${esc(r.match_reason)}</div>
      </td>
      <td class="num">${fmt(r.liked_count)}</td>
      <td class="content">${esc(r.content)}${
        r.reply_to
          ? `<div class="quote">回复 @${esc(r.reply_to)}：${esc(r.reply_content)}</div>`
          : ''
      }</td>
    </tr>`).join('');

  $('#hitBody').innerHTML = `
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>评论时间</th><th>歌曲</th><th>用户</th><th>点赞</th><th>评论内容</th>
        </tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>
    ${rows.length > MAX_ROWS
      ? `<div class="more-note">仅展示前 ${MAX_ROWS} 条，完整 ${rows.length} 条请点「导出 CSV / JSON」。</div>`
      : ''}`;
}

/* ------------------------------------------------------------ 导出 */
function csvCell(v) {
  return '"' + String(v ?? '').replace(/"/g, '""') + '"';
}

function download(filename, text, mime) {
  const blob = new Blob([text], { type: mime });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(a.href);
    a.remove();
  }, 500);
}

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}`;
}

function exportCSV() {
  const rows = (state.snap?.results) || [];
  if (!rows.length) return;
  const head = ['评论时间', '歌曲', '歌曲ID', '昵称', '用户ID', '点赞数', 'IP属地', '命中原因', '评论内容'];
  const lines = [head.map(csvCell).join(',')];
  rows.forEach((r) => {
    lines.push([
      r.time_str, r.song_name, r.song_id, r.nickname, r.user_id,
      r.liked_count, r.ip_location, r.match_reason, r.content,
    ].map(csvCell).join(','));
  });
  // BOM 前缀让 Excel 正确识别 UTF-8 中文
  download(`网易云评论检索结果_${stamp()}.csv`, '\ufeff' + lines.join('\r\n'), 'text/csv;charset=utf-8');
}

function exportJSON() {
  const snap = state.snap;
  if (!snap || !(snap.results || []).length) return;
  const payload = {
    exported_at: new Date().toLocaleString('zh-CN'),
    target: snap.target,
    summary: {
      songs_total: snap.songs_total,
      scanned: snap.scanned,
      hits: snap.hits,
      elapsed_seconds: snap.elapsed,
    },
    songs: snap.songs,
    results: snap.results,
  };
  download(`网易云评论检索结果_${stamp()}.json`,
    JSON.stringify(payload, null, 2), 'application/json;charset=utf-8');
}

/* ------------------------------------------------------------ 事件绑定 */
document.addEventListener('DOMContentLoaded', () => {
  $('#btnResolve').addEventListener('click', resolveSongs);
  $('#btnStart').addEventListener('click', startScan);
  $('#btnStop').addEventListener('click', async () => {
    if (!state.jobId) return;
    try { await post('/api/stop', { id: state.jobId }); } catch (e) { /* 忽略 */ }
  });
  $('#btnSelectAll').addEventListener('click', () => {
    state.selected = new Set(state.items.map((i) => i.song_id));
    renderSongs();
  });
  $('#btnSelectNone').addEventListener('click', () => {
    state.selected = new Set();
    renderSongs();
  });
  $('#btnCsv').addEventListener('click', exportCSV);
  $('#btnJson').addEventListener('click', exportJSON);

  // Ctrl/Cmd + Enter 快捷解析
  $('#query').addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') resolveSongs();
  });

  // 无论页面是从后端托管、别的预览服务器，还是 file:// 双击打开，
  // 都先自动找个能用的后端；找不到时 checkHealth 会给出明确指引。
  checkHealth(true);

  // 每 10 秒静默复查一次连接：后端被关掉时右上角会立刻变红，
  // 不必等到点了按钮才发现「没反应」。任务运行中由轮询负责，这里不重复。
  setInterval(() => {
    if (state.jobId && state.snap && state.snap.status === 'running') return;
    checkHealth(false);
  }, 10000);
});
