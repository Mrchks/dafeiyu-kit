/* 模型与 API：连接列表与用途分配各占一个分页；添加和编辑在固定高度的窗口中进行。
   检测连接、读取模型列表、保存前验证都由控制台直接请求服务商（app/modelprobe.py），不需要先保存。 */
const MODELHUB = {data: null, busy: false};
const MD = {preset: null, models: [], probed: null, pick: '', think: null, thinkOrig: null, thinkChanged: false, probeCtl: null, editing: null};
const THINK_LABEL = {auto: '不指定', off: '关闭', on: '开启', low: '低', medium: '中', high: '高', custom: '自定义'};
const THINK_ORDER = ['auto', 'off', 'on', 'low', 'medium', 'high'];
const THINK_TIP = {auto: '不发送思考参数，由服务商决定。使用中转站时，以中转站的说明文档为准，必要时在「其他参数」中填写。',
  off: '不启用思考，回答最快，用量最小。', on: '启用思考，回答前先推理，回答变慢，用量增加。',
  low: '程度越高，回答越慢，用量越大。日常聊天建议选择「低」。', medium: '适合需要一定推理的问题，回答速度和用量居中。',
  high: '适合复杂问题，回答明显变慢，用量明显增加。', custom: '当前连接写有控制台无法识别的思考参数，保持原样。选择其他档位后将替换为所选档位的参数。'};
const PROBE_SHORT = {auth: '密钥无效或没有权限', notfound: '地址或接口协议不匹配', model: '模型无法使用', quota: '余额不足或请求过于频繁',
  rejected: '服务商拒绝了验证请求', server: '服务商暂时无法处理', timeout: '请求超时', network: '无法连接到服务商', tls: '证书或代理问题',
  busy: '正在检测中', unknown: '验证未完成'};

const presetOf = id => (MODELHUB.data?.presets || []).find(p => p.id === id);
// 列表图标：自定义连接的地址与某个预设相同时，使用该服务商的图标
function rowPreset(m){
  if (m.preset !== 'custom') return presetOf(m.preset);
  let host = ''; try { host = new URL(m.base_url).host; } catch(e){}
  return (MODELHUB.data?.presets || []).find(p => p.base_url && host && new URL(p.base_url).host === host) || presetOf('custom');
}
function thinkSpec(presetId, protocol){
  const p = presetOf(presetId);
  if (p && p.thinking && p.protocol === protocol) return p.thinking;
  return (MODELHUB.data?.thinking_generic || {})[protocol] || null;
}
const thinkOwned = spec => new Set(Object.values(spec?.levels || {}).flatMap(m => Object.keys(m)));
function iconHTML(p, size){
  if (!p || !p.icon) return `<span class="pv-ic ph" aria-hidden="true">+</span>`;
  const mono = !p.icon.endsWith('-color');
  return `<span class="pv-ic" aria-hidden="true"><img src="/icons/providers/${p.icon}.svg" alt=""${mono ? ' class="mono"' : ''}></span>`;
}
function whenText(at){
  if (!at) return ''; const d = new Date(at * 1000), now = new Date(), p2 = n => String(n).padStart(2, '0');
  const t = `${p2(d.getHours())}:${p2(d.getMinutes())}`;
  return d.toDateString() === now.toDateString() ? '今天 ' + t : `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${t}`;
}
function modelOptions(select, models, value){
  select.replaceChildren(...models.map(p => new Option(p.name + ' / ' + p.model, p.id)));
  if (models.some(p => p.id === value)) select.value = value;
}
function modelTab(name){
  $$('#modelTabs button').forEach(b => b.classList.toggle('on', b.dataset.mt2 === name));
  $$('#p-models > .fcol').forEach(t => t.hidden = t.id !== 'mt2-' + name);
}
function modelPane(name){
  $$('#modelDlgTabs button').forEach(b => b.classList.toggle('on', b.dataset.mp === name));
  $$('#modelDlg .mpane').forEach(t => t.hidden = t.id !== 'mp-' + name);
  $('#modelDlg .mdlg-body').scrollTop = 0;
}

/* ---------------- 页面 ---------------- */
async function loadModels(){
  if (MODELHUB.busy) return;
  const r = await api('/api/models');
  if (r.need_login) needLogin(r, $('#modelNeedLogin'), () => skRun($('#p-models'), loadModels));
  if (r.error || !Array.isArray(r.models)) throw new Error(r.error || '模型设置读取失败。');
  MODELHUB.data = r;
  const counts = {};
  for (const s of r.sessions) if (!s.inherited) counts[s.provider] = (counts[s.provider] || 0) + 1;
  const rows = r.models.map(p => {
    const tags = [];
    if (p.id === r.default_chat) tags.push('<span class="pill a">默认聊天</span>');
    if (p.id === r.default_image) tags.push('<span class="pill">识图</span>');
    else if (p.vision) tags.push('<span class="pill">图片输入</span>');
    if (p.thinking) tags.push(`<span class="pill">思考：${THINK_LABEL[p.thinking] || p.thinking}</span>`);
    if (counts[p.id]) tags.push(`<span class="pill">${counts[p.id]} 个会话</span>`);
    const c = p.check;
    const st = !p.key_set ? ['w', '尚未配置密钥', ''] : !c ? ['w', '尚未验证', ''] : c.ok ? ['g', '连接正常 · ' + whenText(c.at), c.message]
      : ['b', (PROBE_SHORT[c.status] || '验证未通过') + ' · ' + whenText(c.at), c.message];
    return `<div class="conn" data-id="${esc(p.id)}"><div class="cn">${iconHTML(rowPreset(p))}<div style="min-width:0"><b>${esc(p.name)}</b><code>${esc(p.model)}</code></div></div>
      <div class="tags">${tags.join('')}</div>
      <div class="st"${st[2] ? ` data-tip="${esc(st[2])}"` : ''}><span class="dot ${st[0]}"></span><span>${esc(st[1])}</span></div>
      <div class="ops"><button class="ghost" data-op="verify">验证</button><button class="ghost" data-op="edit">编辑</button><button class="ghost danger" data-op="del">删除</button></div></div>`;
  }).join('');
  const box = $('#modelRows');
  setHTML(box, rows || '<div class="empty">尚未添加连接<div class="row"><button class="primary" data-op="add">添加连接</button></div></div>');
  box.onclick = e => { const b = e.target.closest('button[data-op]'); if (!b) return; const id = b.closest('.conn')?.dataset.id;
    ({verify: () => modelVerify(id, b), edit: () => modelEditor(id), del: () => modelDelete(id), add: () => modelEditor()})[b.dataset.op](); };
  const enabled = r.models.filter(p => p.enabled);
  modelOptions($('#modelDefaultChat'), enabled, r.default_chat);
  modelOptions($('#modelImage'), enabled.filter(p => p.vision), r.default_image);
  $('#modelUseChat').disabled = !enabled.length;
  $('#modelUseImage').disabled = !enabled.some(p => p.vision);
  const sessions = $('#modelSessions'); sessions.replaceChildren();
  for (const s of r.sessions){
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>' + esc(s.label) + (s.inherited ? '<small>跟随默认聊天模型</small>' : '<small>已单独指定</small>') + '</td><td></td><td></td>';
    const select = document.createElement('select');
    select.setAttribute('aria-label', s.label + '的聊天模型'); modelOptions(select, enabled, s.provider);
    tr.children[1].appendChild(select);
    const b = document.createElement('button'); b.textContent = '应用'; b.disabled = !enabled.length;
    b.onclick = () => modelAssign('session', select.value, s.umo);
    tr.children[2].appendChild(b); sessions.appendChild(tr); enhanceSelect(select);
  }
  if (!r.sessions.length) sessions.innerHTML = '<tr><td colspan="3" class="empty">白名单中暂无群或私聊。可在「常用设置」中添加。</td></tr>';
  $('#modelRoleNote').textContent = '预设核对日期：' + r.checked_on;
}
async function modelVerify(id){
  if (MODELHUB.busy) return;
  MODELHUB.busy = true;
  try {
    const r = await api('/api/models/test', {id});
    if (r.error) throw new Error(r.error);
    infoDlg(r.ok ? '验证通过' : '验证未通过', `<p>${esc(r.message || '验证未完成，请稍后重试。')}</p>`);
  } catch(e){ infoDlg('验证未完成', `<p>${esc(e.message)}</p>`); }
  finally { MODELHUB.busy = false; }
  await loadModels().catch(() => {});
}
async function modelDelete(id){
  const p = MODELHUB.data?.models.find(x => x.id === id); if (!p || MODELHUB.busy) return;
  if (!(await ask(`<h3 style="margin-top:0">删除连接</h3><p>确定删除「${esc(p.name)} / ${esc(p.model)}」吗？删除后需要重新填写密钥才能恢复。</p>`, '删除'))) return;
  MODELHUB.busy = true;
  try {
    const r = await api('/api/models/delete', {id});
    if (r.in_use) return infoDlg('无法删除', `<p>${esc(r.message)}</p>`);
    if (!r.ok) throw new Error(r.error || '删除失败');
    toast('连接已删除', 'ok');
  } catch(e){ infoDlg('删除未完成', `<p>${esc(e.message)}</p>`); }
  finally { MODELHUB.busy = false; }
  await loadModels().catch(() => {});
}
async function modelAssign(role, provider, umo){
  if (MODELHUB.busy || !provider) return;
  MODELHUB.busy = true;
  try {
    const r = await api('/api/models/assign', {role, provider, umo});
    if (!r.ok) throw new Error(r.error || '模型分配失败');
    CHAT.options = null; toast('模型分配已更新', 'ok');
  } catch(e){ toast(e.message, 'err'); }
  finally { MODELHUB.busy = false; }
  await loadModels().catch(() => {});
}

/* ---------------- 添加与编辑窗口 ---------------- */
function provList(){
  const list = $('#modelProvList'), cur = MD.preset?.id, lock = !!MD.editing;
  list.innerHTML = (MODELHUB.data.presets || []).map(p => `<a data-id="${p.id}" role="option" class="${p.id === cur ? 'on' : ''}${lock && p.id !== cur ? ' dis' : ''}"${p.id === cur ? ' aria-selected="true"' : ''}>${iconHTML(p)}<span>${esc(p.name)}</span></a>`).join('');
  list.querySelectorAll('a').forEach(a => a.onclick = () => { if (a.dataset.id !== MD.preset?.id) choosePreset(a.dataset.id); });
  list.classList.add('noanim'); requestAnimationFrame(() => slideInd(list, ':scope > a.on'));
}
function probeBox(kind, text, spin){
  const b = $('#modelProbeBox'); b.className = 'probe' + (kind ? ' ' + kind : '');
  b.innerHTML = (spin ? '<span class="spin"></span>' : `<span class="dot${kind === 'ok' ? ' g' : kind === 'err' ? ' b' : kind === 'warn' ? ' w' : ''}"></span>`) + '<span class="pt"></span>';
  b.querySelector('.pt').textContent = text;
}
function choosePreset(id, saved){
  const p = presetOf(id); if (!p) return;
  MD.preset = p; MD.models = []; MD.probed = null; probeCancel();
  $('#modelEditTitle').textContent = (MD.editing ? '编辑连接 · ' : '添加连接 · ') + p.name;
  if (!saved){
    $('#modelName').value = p.name; $('#modelBase').value = p.base_url; $('#modelCode').value = p.model;
    $('#modelProtocol').value = p.protocol; $('#modelVision').checked = !!p.vision; $('#modelHeaders').value = '';
    $('#modelExtra').value = Object.keys(p.extra_body || {}).length ? JSON.stringify(p.extra_body, null, 2) : '';
    MD.thinkOrig = null; MD.thinkChanged = true;
    const spec = thinkSpec(p.id, p.protocol); MD.think = spec && !spec.always ? (spec.initial || spec.default) : null;
  }
  MD.pick = $('#modelCode').value;
  protoMark(); modelBaseCheck(true); provList();
  probeBox('', p.id === 'custom' ? '使用中转站时，请先在「高级」分页填写 API 基础地址和接口协议，再检测连接。' : '尚未检测。检测会直接请求服务商，可能产生极少量用量。');
  $('#modelKeyNote').innerHTML = p.key_url ? `在 <a href="${esc(p.key_url)}" target="_blank" rel="noopener">${esc(new URL(p.key_url).host)}</a> 获取密钥。` + (MD.editing ? ' 留空则保留原密钥。' : '') : (MD.editing ? '留空则保留原密钥。' : '');
  renderModels(); renderThink(); visionNote();
  if (!saved && p.id === 'custom') modelPane('adv');
}
function protoMark(){ $$('#protoSeg button').forEach(b => b.classList.toggle('on', b.dataset.v === $('#modelProtocol').value)); }
function visionNote(){
  const m = MD.models.find(x => x.id === $('#modelCode').value);
  if (MD.editing && $('#modelCode').value === MD.editing.model && !MD.visionTouched){ $('#modelVisionNote').textContent = '沿用此连接原有的设置，可以手动修改。'; return; }
  $('#modelVisionNote').textContent = m && typeof m.vision === 'boolean' ? '已按服务商返回的型号信息自动判断，可以手动修改。'
    : $('#modelCode').value === MD.preset?.model ? '预设型号的图片能力已确认，可以手动修改。' : '该型号的图片能力尚未确认，确认支持后再开启。';
}
function renderModels(){
  const box = $('#modelList'), q = $('#modelFind').value.trim().toLowerCase(), p = MD.preset;
  let list = MD.models.slice();
  if (p && p.model && !list.some(m => m.id === p.model) && MD.probed && !MD.probed.listed) list.unshift({id: p.model});
  list.sort((a, b) => (b.id === p?.model) - (a.id === p?.model));
  const rows = list.filter(m => m.id.toLowerCase().includes(q));
  $('#modelCount').textContent = MD.probed && MD.probed.listed ? `共 ${rows.length} 个` : '';
  if (!MD.probed){ box.innerHTML = `<div class="mempty">检测连接后在此列出可用模型。当前模型 ID：${esc($('#modelCode').value || '尚未填写')}，可以在「高级」分页修改。</div>`; return; }
  if (!rows.length){ box.innerHTML = `<div class="mempty">${MD.probed.listed ? '没有符合筛选条件的模型' : '该服务商未提供模型列表，请在「高级」分页手动填写模型 ID'}</div>`; return; }
  box.innerHTML = rows.map((m, i) => {
    const caps = [m.id === p?.model ? '<span class="pill a">推荐</span>' : '', (m.vision || (m.id === p?.model && p.vision && m.vision !== false)) ? '<span class="pill">识图</span>' : '', m.reasoning ? '<span class="pill">思考</span>' : ''].join('');
    return `<div class="mopt${m.id === MD.pick ? ' on' : ''}" role="option" aria-selected="${m.id === MD.pick}" data-id="${esc(m.id)}" style="animation-delay:${Math.min(i, 12) * 20}ms"><span class="rd"></span><code>${esc(m.id)}</code><span class="caps">${caps}</span></div>`;
  }).join('');
  box.querySelectorAll('.mopt').forEach(d => d.onclick = () => pickModel(d.dataset.id));
}
function pickModel(id){
  MD.pick = id; $('#modelCode').value = id; MD.visionTouched = true;
  $$('#modelList .mopt').forEach(x => { const on = x.dataset.id === id; x.classList.toggle('on', on); x.setAttribute('aria-selected', on); });
  const m = MD.models.find(x => x.id === id);
  if (m && typeof m.vision === 'boolean') $('#modelVision').checked = m.vision;
  else $('#modelVision').checked = id === MD.preset?.model ? !!MD.preset.vision : false;
  visionNote();
}
function renderThink(){
  const spec = thinkSpec(MD.preset?.id, $('#modelProtocol').value), seg = $('#thinkSeg'), none = $('#thinkNone');
  if (!spec || spec.always){
    seg.hidden = true; none.hidden = false; $('#thinkTip').textContent = '';
    none.textContent = spec && spec.always ? '该型号始终思考，服务商不提供思考程度选项。' : '该型号不提供思考程度选项。';
    return;
  }
  seg.hidden = false; none.hidden = true;
  const levels = THINK_ORDER.filter(l => l in spec.levels);
  if (MD.think === 'custom') levels.push('custom');
  if (!levels.includes(MD.think)) MD.think = spec.initial || spec.default;
  seg.innerHTML = levels.map(l => `<button data-v="${l}" class="${l === MD.think ? 'on' : ''}">${THINK_LABEL[l]}</button>`).join('');
  seg.querySelectorAll('button').forEach(b => b.onclick = () => { if (b.dataset.v === 'custom') return; MD.think = b.dataset.v; MD.thinkChanged = MD.think !== MD.thinkOrig; seg.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); thinkTipText(spec); });
  thinkTipText(spec);
}
function thinkTipText(spec){
  let t = THINK_TIP[MD.think] || '';
  if (MD.think === spec.default && MD.think !== 'auto') t += '（此为服务商默认档位）';
  $('#thinkTip').textContent = t;
}
function modelEditor(id){
  if (!MODELHUB.data || MODELHUB.busy) return;
  const p = id ? MODELHUB.data.models.find(x => x.id === id) : null;
  MD.editing = p || null; MD.visionTouched = false; $('#modelEditID').value = p ? p.id : '';
  $('#modelSaveNote').textContent = '保存时会对所选模型再验证一次。'; $('#modelSaveNote').style.color = ''; $('#modelForce').hidden = true;
  $('#modelKey').value = ''; $('#modelKey').type = 'password'; $('#modelKeyEye').classList.remove('on'); $('#modelFind').value = '';
  if (p){
    const preset = presetOf(p.preset) || presetOf('custom');
    $('#modelName').value = p.name; $('#modelBase').value = p.base_url; $('#modelCode').value = p.model;
    $('#modelProtocol').value = p.protocol; $('#modelVision').checked = p.vision; $('#modelHeaders').value = '';
    const spec = thinkSpec(preset.id, p.protocol), extra = {...(p.extra_body || {})};
    if (p.thinking && p.thinking !== 'custom' && spec && spec.where === 'extra') for (const k of thinkOwned(spec)) delete extra[k];
    $('#modelExtra').value = Object.keys(extra).length ? JSON.stringify(extra, null, 2) : '';
    MD.think = p.thinking; MD.thinkOrig = p.thinking; MD.thinkChanged = false;
    $('#modelKey').placeholder = '留空则保留原密钥；填写后替换';
    choosePreset(preset.id, true);
    if (p.shared_source) $('#modelKeyNote').textContent = '此连接由多个型号共用，修改地址或密钥会同时影响这些型号。留空则保留原密钥。';
  } else {
    $('#modelKey').placeholder = '填写 API Key';
    choosePreset('deepseek');
  }
  modelPane('basic'); $('#modelDlg').showModal();
  requestAnimationFrame(() => slideInd($('#modelProvList'), ':scope > a.on'));
}
// API 基础地址：自定义校验，不使用浏览器的校验气泡
function modelBaseCheck(quiet){
  const v = $('#modelBase').value.trim(), ok = !v || /^https?:\/\/[^\s/?#]+[^\s?#]*$/i.test(v);
  const msg = ok ? '' : '请填写以 http:// 或 https:// 开头、不含查询参数的地址，例如 https://api.example.com/v1';
  if (!quiet || ok){ $('#modelBaseErr').textContent = msg; $('#modelBase').classList.toggle('bad', !ok); }
  return ok;
}
function draftBody(){
  let extra = {}, headers;
  try { extra = JSON.parse($('#modelExtra').value.trim() || '{}'); if (!extra || typeof extra !== 'object' || Array.isArray(extra)) throw 0; }
  catch(e){ throw new Error('「其他参数」不是有效的 JSON 对象。'); }
  try { if ($('#modelHeaders').value.trim()){ headers = JSON.parse($('#modelHeaders').value); if (!headers || typeof headers !== 'object' || Array.isArray(headers)) throw 0; } }
  catch(e){ throw new Error('「额外请求头」不是有效的 JSON 对象。'); }
  const body = {id: $('#modelEditID').value, preset: MD.preset.id, name: $('#modelName').value.trim(), key: $('#modelKey').value.trim(),
    base_url: $('#modelBase').value.trim(), protocol: $('#modelProtocol').value, model: $('#modelCode').value.trim(),
    vision: $('#modelVision').checked, extra_body: extra};
  if (headers) body.headers = headers;
  const spec = thinkSpec(MD.preset.id, body.protocol);
  if (spec && !spec.always && MD.think){
    body.thinking = !MD.editing || MD.thinkChanged ? MD.think : (MD.think === 'custom' ? 'custom' : 'keep');
    if (body.thinking === 'custom') delete body.thinking;
  }
  return body;
}
function probeCancel(){ if (MD.probeCtl){ MD.probeCtl.abort(); MD.probeCtl = null; } $('#modelProbe').textContent = '检测连接'; }
async function modelProbe(){
  if (MD.probeCtl){ probeCancel(); probeBox('', '已取消检测。'); return; }
  if (!modelBaseCheck()){ modelPane('adv'); return; }
  let body; try { body = draftBody(); } catch(e){ probeBox('err', e.message); return; }
  if (!body.key && !body.id){ probeBox('warn', '请先填写 API Key。'); $('#modelKey').focus(); return; }
  if (!body.base_url){ probeBox('warn', '请先在「高级」分页填写 API 基础地址。'); return; }
  const ctl = new AbortController(); MD.probeCtl = ctl; $('#modelProbe').textContent = '取消检测';
  probeBox('', '正在检测连接并读取模型列表…', true);
  const timer = setTimeout(() => ctl.abort(), 18000);
  try {
    const res = await fetch('/api/models/probe', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal: ctl.signal});
    const r = await res.json();
    if (MD.probeCtl !== ctl) return;
    if (r.error) throw new Error(r.error);
    MD.probed = r; MD.models = r.models || [];
    probeBox(r.ok ? (r.listed ? 'ok' : 'warn') : 'err', r.message);
    if (r.ok){ renderModels();
      // 当前模型不在列表中时才自动改选推荐型号；已有选择保持不变，不改动图片输入等设置
      if (!MD.models.some(m => m.id === MD.pick) && MD.models.some(m => m.id === MD.preset.model) && !MD.editing) pickModel(MD.preset.model); else visionNote(); }
  } catch(e){ if (MD.probeCtl === ctl) probeBox('err', e.name === 'AbortError' ? '检测超时或已取消。' : e.message); }
  finally { clearTimeout(timer); if (MD.probeCtl === ctl){ MD.probeCtl = null; $('#modelProbe').textContent = '检测连接'; } }
}
async function modelSave(skipVerify){
  if (MODELHUB.busy) return;
  const note = $('#modelSaveNote'), say = (t, bad) => { note.textContent = t; note.style.color = bad ? 'var(--bad)' : ''; };
  if (!modelBaseCheck()){ modelPane('adv'); $('#modelBase').focus(); return; }
  let body; try { body = draftBody(); } catch(e){ modelPane('adv'); say(e.message, true); return; }
  if (!body.model){ say('请选择或填写模型 ID。', true); return; }
  if (!body.key && !body.id){ modelPane('basic'); say('请填写 API Key。', true); $('#modelKey').focus(); return; }
  const spec = thinkSpec(body.preset, body.protocol);
  const clash = spec && spec.where === 'extra' && body.thinking && [...thinkOwned(spec)].filter(k => k in body.extra_body);
  body.verify = true; if (skipVerify === true) body.skip_verify = true;
  MODELHUB.busy = true; $('#modelForce').hidden = true; say(skipVerify === true ? '正在保存…' : '正在验证所选模型并保存…');
  try {
    const r = await api('/api/models/save', body);
    if (r.verify_failed){ say('验证未通过：' + r.message, true); $('#modelForce').hidden = false; return; }
    if (!r.ok) throw new Error(r.error || '保存失败');
    $('#modelKey').value = ''; $('#modelDlg').close(); CHAT.options = null;
    toast((r.verified ? '连接已保存并验证通过' : '连接已保存') + '\n现有会话尚未切换，可在「用途分配」中应用。' + (clash && clash.length ? '「其他参数」中的思考相关参数已按思考程度选项处理。' : ''), 'ok');
  } catch(e){ say(e.message + '。填写内容已保留。', true); }
  finally { MODELHUB.busy = false; }
  if (!$('#modelDlg').open) await loadModels().catch(() => {});
}

/* ---------------- 配置说明 ---------------- */
function docRender(md){
  const toc = [], out = []; let list = null;
  const inline = x => esc(x).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/(https:\/\/[A-Za-z0-9.\-_/~%?=&#]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  const close = () => { if (list){ out.push(`</${list}>`); list = null; } };
  for (const line of md.split('\n')){
    let m;
    if ((m = line.match(/^#\s+/))){ close(); continue; }
    if ((m = line.match(/^##\s+(.*)/))){ close(); toc.push(m[1]); out.push(`<h2 id="doc-${toc.length}">${inline(m[1])}</h2>`); continue; }
    if ((m = line.match(/^###\s+(.*)/))){ close(); out.push(`<h3>${inline(m[1])}</h3>`); continue; }
    if ((m = line.match(/^\s*[-*]\s+(.*)/))){ if (list !== 'ul'){ close(); out.push('<ul>'); list = 'ul'; } out.push(`<li>${inline(m[1])}</li>`); continue; }
    if ((m = line.match(/^\s*\d+\.\s+(.*)/))){ if (list !== 'ol'){ close(); out.push('<ol>'); list = 'ol'; } out.push(`<li>${inline(m[1])}</li>`); continue; }
    close(); if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  close();
  return {toc, html: out.join('')};
}
async function docOpen(){
  const d = $('#docDlg'), body = $('#docBody'); body.innerHTML = ''; $('#docToc').innerHTML = ''; body.classList.add('skhost'); d.showModal();
  await skOnce(body, async () => {
    const r = await api('/api/doc/models'); if (!r.text) throw new Error(r.error || '配置说明读取失败。');
    const {toc, html} = docRender(r.text); body.innerHTML = html;
    $('#docToc').innerHTML = toc.map((t, i) => `<a data-i="${i + 1}" class="${i ? '' : 'on'}">${esc(t)}</a>`).join('');
    $$('#docToc a').forEach(a => a.onclick = () => { body.querySelector('#doc-' + a.dataset.i).scrollIntoView({block: 'start'}); });
    requestAnimationFrame(() => slideInd($('#docToc'), ':scope > a.on'));
  });
}
$('#docBody').addEventListener('scroll', () => {
  const body = $('#docBody'), hs = [...body.querySelectorAll('h2')]; let cur = 1;
  for (const h of hs) if (h.offsetTop - body.scrollTop < 60) cur = +h.id.slice(4);
  $$('#docToc a').forEach(a => a.classList.toggle('on', +a.dataset.i === cur)); slideInd($('#docToc'), ':scope > a.on');
});

/* ---------------- 事件 ---------------- */
$$('#modelTabs button').forEach(b => b.onclick = () => modelTab(b.dataset.mt2));
$$('#modelDlgTabs button').forEach(b => b.onclick = () => modelPane(b.dataset.mp));
$$('#protoSeg button').forEach(b => b.onclick = () => { $('#modelProtocol').value = b.dataset.v; protoMark(); MD.models = []; MD.probed = null;
  const spec = thinkSpec(MD.preset.id, b.dataset.v); MD.think = spec && !spec.always ? (spec.initial || spec.default) : null; MD.thinkChanged = true; renderModels(); renderThink(); });
$('#modelAdd').onclick = () => modelEditor();
$('#modelDoc').onclick = docOpen; $('#docClose').onclick = () => $('#docDlg').close();
$('#modelClose').onclick = () => { if (!MODELHUB.busy) $('#modelDlg').close(); };
$('#modelDlg').addEventListener('close', () => { probeCancel(); $('#modelKey').value = ''; });
$('#modelRefresh').onclick = () => { apiFlush(); return loadModels().catch(e => toast('刷新失败\n' + e.message, 'err')); };
$('#modelProbe').onclick = modelProbe;
$('#modelSave').onclick = () => modelSave(); $('#modelForce').onclick = () => modelSave(true);
$('#modelFind').oninput = renderModels;
$('#modelKeyEye').onclick = () => { const k = $('#modelKey'), show = k.type === 'password'; k.type = show ? 'text' : 'password'; $('#modelKeyEye').classList.toggle('on', show); $('#modelKeyEye').setAttribute('aria-label', show ? '隐藏密钥' : '显示密钥'); };
$('#modelKey').addEventListener('paste', e => { const t = (e.clipboardData || window.clipboardData)?.getData('text'); if (t == null) return; e.preventDefault(); $('#modelKey').setRangeText(t.trim(), $('#modelKey').selectionStart, $('#modelKey').selectionEnd, 'end'); });
$('#modelBase').addEventListener('blur', () => modelBaseCheck());
$('#modelBase').addEventListener('input', () => { if ($('#modelBase').classList.contains('bad')) modelBaseCheck(); MD.probed = null; MD.models = []; renderModels(); });
$('#modelCode').oninput = () => { MD.pick = $('#modelCode').value.trim(); $$('#modelList .mopt').forEach(x => x.classList.toggle('on', x.dataset.id === MD.pick)); if (MD.pick !== MD.preset?.model) $('#modelVision').checked = false; visionNote(); };
$('#modelUseChat').onclick = () => modelAssign('chat', $('#modelDefaultChat').value);
$('#modelUseImage').onclick = () => modelAssign('image', $('#modelImage').value);
$('#modelApplyRules').onclick = async () => {
  if (MODELHUB.busy) return; MODELHUB.busy = true;
  try { const r = await api('/api/models/image-rules', {}); if (!r.ok) throw new Error(r.error || '识图规则未更新');
    toast('识图规则已更新\n下次识图将按新规则生成描述，旧的描述缓存不会混用。', 'ok');
  } catch(e){ toast(e.message, 'err'); } finally { MODELHUB.busy = false; }
};

if (curPage === 'models') skRun($('#p-models'), loadModels);
