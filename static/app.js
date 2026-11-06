/* 工作记录 - 前端逻辑（原生 JS，无外部依赖，可离线运行） */
let state = {
  records: [], currentId: null, dirty: false, config: null,
  view: 'calendar', calYear: 0, calMonth: 0
};

const $ = id => document.getElementById(id);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function highlight(text, kw) {
  const t = String(text ?? '');
  const i = t.toLowerCase().indexOf(kw.toLowerCase());
  if (i < 0) return esc(t);
  return esc(t.slice(0, i)) + '<mark>' + esc(t.slice(i, i + kw.length)) + '</mark>' + esc(t.slice(i + kw.length));
}

function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

let toastTimer = null;
function toast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 1800);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  return res.json();
}

function pad2(n) { return String(n).padStart(2, '0'); }
function today() {
  const d = new Date();
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}
function fmtDate(d) {
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}
function trunc(s, n) {
  s = String(s ?? '');
  return s.length > n ? s.slice(0, n) + '…' : s;
}

/* ---------- 初始化 ---------- */
async function init() {
  state.config = await api('/api/config');
  $('dataDir').textContent = '数据目录：' + state.config.data_dir;
  const now = new Date();
  state.calYear = now.getFullYear();
  state.calMonth = now.getMonth() + 1;
  fillCategories();
  bindEvents();
  await loadRecords();
  await renderCalendar();
  await checkReminders();
  showView('calendar');   // 开屏默认日历
}

function showView(v) {
  state.view = v;
  $('btnViewCalendar').classList.toggle('primary', v === 'calendar');
  $('btnViewList').classList.toggle('primary', v === 'list');
  $('calendarView').classList.toggle('hidden', v !== 'calendar');
  $('listView').classList.toggle('hidden', v !== 'list');
  if (v === 'calendar') renderCalendar();
}

function fillCategories() {
  const selF = $('fCategory'), selE = $('category');
  const keepF = selF.value, keepE = selE.value;
  const opts = state.config.categories.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  selF.innerHTML = '<option value="">全部类别</option>' + opts;
  selE.innerHTML = '<option value="">（未分类）</option>' + opts;
  selF.value = keepF; selE.value = keepE;
}

function bindEvents() {
  let debounce = null;
  $('kw').addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(loadRecords, 300);
  });
  $('fCategory').addEventListener('change', loadRecords);
  $('fStart').addEventListener('change', loadRecords);
  $('fEnd').addEventListener('change', loadRecords);
  $('fDone').addEventListener('change', loadRecords);
  $('btnResetFilter').addEventListener('click', resetFilters);
  $('btnViewCalendar').addEventListener('click', () => showView('calendar'));
  $('btnViewList').addEventListener('click', () => showView('list'));
  $('calPrev').addEventListener('click', () => shiftMonth(-1));
  $('calNext').addEventListener('click', () => shiftMonth(1));
  $('calToday').addEventListener('click', gotoToday);
  $('calGrid').addEventListener('click', onCalClick);
  $('recordList').addEventListener('click', e => {
    const delBtn = e.target.closest('button.del-rec');
    if (delBtn) {
      e.stopPropagation();
      deleteRecordById(Number(delBtn.dataset.id), delBtn.dataset.title);
      return;
    }
    const item = e.target.closest('.item');
    if (item) selectRecord(Number(item.dataset.id));
  });
  $('btnNew').addEventListener('click', newRecord);
  $('btnSave').addEventListener('click', save);
  $('btnDelete').addEventListener('click', deleteRecord);
  $('btnUpload').addEventListener('click', () => $('fileInput').click());
  $('fileInput').addEventListener('change', e => { uploadFiles(e.target.files); e.target.value = ''; });
  $('btnOpenFolder').addEventListener('click', () => openFolder(state.currentId));
  $('btnCat').addEventListener('click', openCatModal);
  $('btnCatClose').addEventListener('click', closeCatModal);
  $('btnCatAdd').addEventListener('click', addCat);
  $('newCat').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addCat(); } });
  $('catModal').addEventListener('click', e => { if (e.target === $('catModal')) closeCatModal(); });
  $('catList').addEventListener('click', e => {
    const btn = e.target.closest('button.del');
    if (btn) removeCat(btn.dataset.name);
  });
  ['title', 'date', 'category', 'tags', 'content'].forEach(id =>
    $(id).addEventListener('input', () => { state.dirty = true; }));
  ['done', 'deadline'].forEach(id =>
    $(id).addEventListener('change', () => { state.dirty = true; }));
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      if (state.currentId) save();
    }
  });
  $('fileList').addEventListener('click', e => {
    const btn = e.target.closest('button.del');
    if (btn) delFile(btn.dataset.name);
  });
  $('btnExport').addEventListener('click', openExportModal);
  $('btnExpClose').addEventListener('click', closeExportModal);
  $('btnExpWeek').addEventListener('click', () => doExport('week'));
  $('btnExpRange').addEventListener('click', () => doExport('range'));
  $('btnExpYear').addEventListener('click', () => doExport('year'));
  $('expYearSel').addEventListener('change', updateYearCount);
  $('exportModal').addEventListener('click', e => { if (e.target === $('exportModal')) closeExportModal(); });
  const fb = $('filesBox');
  ['dragenter', 'dragover'].forEach(ev => fb.addEventListener(ev, e => {
    e.preventDefault();
    if (state.currentId) fb.classList.add('dragover');
  }));
  fb.addEventListener('dragleave', e => {
    if (!fb.contains(e.relatedTarget)) fb.classList.remove('dragover');
  });
  fb.addEventListener('drop', e => {
    e.preventDefault();
    fb.classList.remove('dragover');
    if (!state.currentId) { toast('请先选择或新建一条记录，再拖入文件'); return; }
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  });
}

/* ---------- 日历视图 ---------- */
function shiftMonth(n) {
  let m = state.calMonth + n, y = state.calYear;
  if (m < 1) { m = 12; y--; }
  if (m > 12) { m = 1; y++; }
  state.calYear = y;
  state.calMonth = m;
  renderCalendar();
}

function gotoToday() {
  const now = new Date();
  state.calYear = now.getFullYear();
  state.calMonth = now.getMonth() + 1;
  renderCalendar();
  $('fStart').value = today();
  $('fEnd').value = today();
  loadRecords();
  showView('list');
}

async function renderCalendar() {
  $('calTitle').textContent = state.calYear + '年' + state.calMonth + '月';
  const monthStr = state.calYear + '-' + pad2(state.calMonth);
  const lastDay = new Date(state.calYear, state.calMonth, 0).getDate();
  const start = monthStr + '-01';
  const end = monthStr + '-' + pad2(lastDay);
  const d = await api('/api/records?start=' + start + '&end=' + end);
  const byDay = {}, byDl = {};
  for (const r of d.records) {
    (byDay[r.date] = byDay[r.date] || []).push(r);
    if (r.deadline && r.deadline.slice(0, 7) === monthStr) {
      (byDl[r.deadline] = byDl[r.deadline] || []).push(r);
    }
  }
  const first = new Date(state.calYear, state.calMonth - 1, 1);
  const lead = (first.getDay() + 6) % 7;  // 周一起头
  const monday0 = new Date(state.calYear, state.calMonth - 1, 1 - lead);
  const total = Math.ceil((lead + lastDay) / 7) * 7;
  const tStr = today();
  let html = '';
  for (let i = 0; i < total; i++) {
    const dt = new Date(monday0);
    dt.setDate(monday0.getDate() + i);
    const ds = fmtDate(dt);
    const inMonth = dt.getMonth() === state.calMonth - 1;
    let cell = `<div class="cal-cell${inMonth ? '' : ' dim'}${ds === tStr ? ' today' : ''}" data-date="${ds}">`;
    cell += `<div class="cal-date">${dt.getDate()}</div>`;
    for (const r of (byDay[ds] || [])) {
      cell += `<div class="cal-item${r.done ? ' done' : ''}" data-id="${r.id}" title="${esc(r.title)}">${r.done ? '✓ ' : ''}${esc(trunc(r.title, 9))}</div>`;
    }
    for (const r of (byDl[ds] || [])) {
      const od = !r.done && ds < tStr;
      cell += `<div class="cal-item deadline${od ? ' overdue' : ''}" data-id="${r.id}" title="截止：${esc(r.title)}">⏰${esc(trunc(r.title, 8))}</div>`;
    }
    cell += '</div>';
    html += cell;
  }
  $('calGrid').innerHTML = html;
}

async function onCalClick(e) {
  const item = e.target.closest('.cal-item');
  if (item) {
    await selectRecord(Number(item.dataset.id));
    return;
  }
  const cell = e.target.closest('.cal-cell');
  if (cell) {
    $('fStart').value = cell.dataset.date;
    $('fEnd').value = cell.dataset.date;
    await loadRecords();
    showView('list');
  }
}

/* ---------- 到期提醒 ---------- */
async function checkReminders() {
  const d = await api('/api/reminders');
  const bar = $('remindBar');
  if (!d.overdue.length && !d.soon.length) {
    bar.classList.add('hidden');
    return;
  }
  bar.innerHTML = `⏰ <strong>${d.overdue.length}</strong> 项已超期，<strong>${d.soon.length}</strong> 项 3 天内截止` +
    ` <button id="btnRemindView" class="mini">查看未完成</button>` +
    ` <button id="btnRemindClose" class="mini">知道了</button>`;
  bar.classList.remove('hidden');
  $('btnRemindView').onclick = () => {
    $('fDone').checked = true;
    loadRecords();
    showView('list');
  };
  $('btnRemindClose').onclick = () => bar.classList.add('hidden');
}

/* ---------- 记录列表 ---------- */
async function loadRecords() {
  const params = new URLSearchParams();
  if ($('kw').value.trim()) params.set('kw', $('kw').value.trim());
  if ($('fCategory').value) params.set('category', $('fCategory').value);
  if ($('fStart').value) params.set('start', $('fStart').value);
  if ($('fEnd').value) params.set('end', $('fEnd').value);
  if ($('fDone').checked) params.set('done', '0');
  const d = await api('/api/records?' + params);
  state.records = d.records;
  renderList();
  updateFilterBar();
}

function updateFilterBar() {
  const parts = [];
  if ($('fStart').value || $('fEnd').value) {
    parts.push('日期 ' + ($('fStart').value || '最早') + ' ~ ' + ($('fEnd').value || '至今'));
  }
  if ($('fCategory').value) parts.push('类别：' + $('fCategory').value);
  if ($('fDone').checked) parts.push('仅未完成');
  if ($('kw').value.trim()) parts.push('搜索“' + $('kw').value.trim() + '”');
  const bar = $('filterBar');
  if (!parts.length) { bar.classList.add('hidden'); return; }
  $('filterBarText').textContent = '筛选中：' + parts.join('，');
  bar.classList.remove('hidden');
}

async function resetFilters() {
  $('kw').value = '';
  $('fCategory').value = '';
  $('fStart').value = '';
  $('fEnd').value = '';
  $('fDone').checked = false;
  await loadRecords();
  toast('已显示全部记录');
}

function renderList() {
  const box = $('recordList');
  if (!state.records.length) {
    box.innerHTML = '<div class="list-empty">没有匹配的记录</div>';
    return;
  }
  const kw = $('kw').value.trim();
  const tStr = today();
  box.innerHTML = state.records.map(r => {
    const marks = [];
    if (r.match_title) marks.push('标题');
    if (r.match_content) marks.push('正文');
    if (r.match_tags) marks.push('标签');
    if (r.match_files) marks.push('资料×' + r.match_files);
    const title = (kw && r.match_title) ? highlight(r.title, kw) : esc(r.title);
    let dl = '';
    if (r.deadline) {
      const od = !r.done && r.deadline < tStr;
      dl = ` · <span class="dl-badge${od ? ' dl-overdue' : ''}">截止${esc(r.deadline.slice(5))}${od ? ' 超期!' : ''}</span>`;
    }
    return `<div class="item ${r.id === state.currentId ? 'active' : ''}" data-id="${r.id}">
      <div class="item-title${r.done ? ' done' : ''}">${r.done ? '✓ ' : ''}${title}</div>
      <div class="item-meta">${esc(r.date)} · ${esc(r.category || '未分类')}${r.tags ? ' · ' + esc(r.tags) : ''}${dl}${marks.length ? ' <span class="badge">' + marks.join(' ') + '</span>' : ''}</div>
      <button class="del-rec" data-id="${r.id}" data-title="${esc(r.title)}" title="删除这条记录">✕</button>
    </div>`;
  }).join('');
}

/* ---------- 编辑 ---------- */
async function selectRecord(id) {
  if (state.dirty && state.currentId === id) { showView('list'); return; }
  if (!await confirmDiscard()) return;
  const d = await api('/api/records/' + id);
  if (!d.record) return;
  state.currentId = id;
  state.dirty = false;
  $('title').value = d.record.title;
  $('date').value = d.record.date;
  $('done').checked = !!d.record.done;
  $('deadline').value = d.record.deadline || '';
  $('category').value = d.record.category || '';
  $('tags').value = d.record.tags || '';
  $('content').value = d.record.content || '';
  $('savedAt').textContent = d.record.updated ? '最近更新 ' + d.record.updated : '';
  $('editor').classList.remove('hidden');
  $('empty').classList.add('hidden');
  showView('list');
  renderList();
  loadFiles();
}

function showEmpty() {
  $('editor').classList.add('hidden');
  $('empty').classList.remove('hidden');
}

async function confirmDiscard() {
  if (!state.dirty) return true;
  return confirm('当前记录有未保存的修改，确定放弃吗？');
}

async function newRecord() {
  if (!await confirmDiscard()) return;
  const d = await api('/api/records', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: '', date: today(), category: '', tags: '', content: '', done: 0, deadline: '' })
  });
  await loadRecords();
  await selectRecord(d.id);
  showView('list');
  $('title').focus();
}

async function save() {
  if (!state.currentId) return;
  const d = await api('/api/records/' + state.currentId, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      title: $('title').value, date: $('date').value,
      done: $('done').checked ? 1 : 0,
      deadline: $('deadline').value,
      category: $('category').value, tags: $('tags').value,
      content: $('content').value
    })
  });
  if (d.ok) {
    state.dirty = false;
    $('savedAt').textContent = '已保存 ' + new Date().toLocaleString();
    toast('已保存');
    await loadRecords();
    loadFiles();
    renderCalendar();
    checkReminders();
  }
}

async function deleteRecordById(id, title) {
  if (!confirm('删除记录「' + title + '」？')) return;
  const delFiles = confirm('是否同时删除资料文件夹？\n「确定」= 彻底删除资料文件\n「取消」= 资料文件保留在磁盘上');
  await api('/api/records/' + id + '?delete_files=' + (delFiles ? 1 : 0), { method: 'DELETE' });
  if (state.currentId === id) {
    state.currentId = null;
    state.dirty = false;
    showEmpty();
  }
  await loadRecords();
  renderCalendar();
  checkReminders();
  toast('已删除');
}

async function deleteRecord() {
  if (!state.currentId) return;
  if (!confirm('确定删除这条记录？')) return;
  const delFiles = confirm('是否同时删除资料文件夹？\n「确定」= 彻底删除资料文件\n「取消」= 资料文件保留在磁盘上');
  await api('/api/records/' + state.currentId + '?delete_files=' + (delFiles ? 1 : 0), { method: 'DELETE' });
  state.currentId = null;
  state.dirty = false;
  showEmpty();
  await loadRecords();
  renderCalendar();
  checkReminders();
  toast('已删除');
}

/* ---------- 资料文件 ---------- */
async function loadFiles() {
  if (!state.currentId) return;
  const d = await api('/api/records/' + state.currentId + '/files');
  const box = $('fileList');
  if (!d.files.length) {
    box.innerHTML = '<li class="muted" style="padding:8px 16px">暂无资料 —— 点「上传文件」，或直接把文件拖到本区域</li>';
    return;
  }
  box.innerHTML = d.files.map(f =>
    `<li>
       <a href="/api/records/${state.currentId}/files/${encodeURIComponent(f.name)}" target="_blank" title="${esc(f.name)}">${esc(f.name)}</a>
       <span class="muted">${fmtSize(f.size)} · ${f.mtime}</span>
       <span class="spacer"></span>
       <button class="link danger del" data-name="${esc(f.name)}">删除</button>
     </li>`).join('');
}

async function uploadFiles(fileList) {
  if (!state.currentId || !fileList.length) return;
  toast('正在上传…');
  for (const f of fileList) {
    const buf = await f.arrayBuffer();
    await api('/api/records/' + state.currentId + '/files', {
      method: 'POST',
      headers: { 'X-Filename': encodeURIComponent(f.name) },
      body: buf
    });
  }
  toast('上传完成');
  loadFiles();
  loadRecords();
}

async function delFile(name) {
  if (!confirm('删除文件「' + name + '」？')) return;
  await api('/api/records/' + state.currentId + '/files/' + encodeURIComponent(name), { method: 'DELETE' });
  loadFiles();
  loadRecords();
}

async function openFolder(id) {
  const d = await api('/api/records/' + id + '/open_folder', { method: 'POST' });
  if (!d.ok) toast('未能自动打开，请手动访问：' + state.config.data_dir);
}

/* ---------- 类别管理 ---------- */
function openCatModal() {
  renderCatList();
  $('newCat').value = '';
  $('catModal').classList.remove('hidden');
  setTimeout(() => $('newCat').focus(), 50);
}

function closeCatModal() {
  $('catModal').classList.add('hidden');
}

function renderCatList() {
  $('catList').innerHTML = state.config.categories.map(c =>
    `<div class="cat-row"><span>${esc(c)}</span><span class="spacer"></span>` +
    `<button class="link danger del" data-name="${esc(c)}">删除</button></div>`
  ).join('') || '<div class="muted" style="padding:8px 4px">暂无类别，请在下方添加</div>';
}

async function saveCats(cats) {
  const d = await api('/api/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ categories: cats })
  });
  state.config.categories = d.categories;
  fillCategories();
}

async function addCat() {
  const name = $('newCat').value.trim();
  if (!name) return;
  if (state.config.categories.includes(name)) { toast('该类别已存在'); return; }
  await saveCats(state.config.categories.concat([name]));
  $('newCat').value = '';
  renderCatList();
  $('newCat').focus();
  toast('已添加「' + name + '」');
}

async function removeCat(name) {
  if (!confirm('删除类别「' + name + '」？已有记录不受影响。')) return;
  await saveCats(state.config.categories.filter(c => c !== name));
  renderCatList();
  toast('已删除「' + name + '」');
}

/* ---------- 导出总结 ---------- */
let expInfo = null;

function closeExportModal() {
  $('exportModal').classList.add('hidden');
}

function updateYearCount() {
  const y = $('expYearSel').value;
  $('expYearCount').textContent = '共 ' + ((expInfo && expInfo.year_counts[y]) || 0) + ' 条';
}

async function openExportModal() {
  const params = new URLSearchParams();
  if ($('fStart').value) params.set('start', $('fStart').value);
  if ($('fEnd').value) params.set('end', $('fEnd').value);
  expInfo = await api('/api/export/info?' + params);
  $('expWeekLabel').innerHTML = esc(expInfo.week.label) + ' <span class="muted">共 ' + expInfo.week.count + ' 条</span>';
  const rb = expInfo.range;
  $('btnExpRange').disabled = !rb;
  $('expRangeLabel').innerHTML = rb
    ? esc(rb.label) + ' <span class="muted">共 ' + rb.count + ' 条</span>'
    : '<span class="muted">左侧日期筛选：先在左侧选好起止日期，即可导出任意时间段（比如补导上周的）</span>';
  const curYear = String(new Date().getFullYear());
  const ys = Object.keys(expInfo.year_counts);
  if (!ys.includes(curYear)) ys.unshift(curYear);
  $('expYearSel').innerHTML = ys.map(y => `<option value="${esc(y)}">${esc(y)} 年</option>`).join('');
  $('expYearSel').value = curYear;
  updateYearCount();
  $('exportModal').classList.remove('hidden');
}

function doExport(scope) {
  const p = new URLSearchParams({ scope, dedup: $('expDedup').checked ? '1' : '0' });
  if (scope === 'week' && expInfo) {
    p.set('start', expInfo.week.start);
    p.set('end', expInfo.week.end);
  }
  if (scope === 'range') {
    p.set('start', $('fStart').value);
    p.set('end', $('fEnd').value);
  }
  if (scope === 'year') p.set('year', $('expYearSel').value);
  const a = document.createElement('a');
  a.href = '/api/export?' + p;
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast('已导出 txt，并存档到 数据目录/导出/ 文件夹');
  closeExportModal();
}

init();
