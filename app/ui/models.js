/* 模型与 API：连接列表和用途分配各占一个分页；添加和编辑在固定高度的窗口中进行。 */
const MODELHUB = {data:null, busy:false};
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
async function loadModels(){
  if (MODELHUB.busy) return;
  const r = await api('/api/models');
  if (r.need_login) needLogin(r, $('#modelNeedLogin'), () => skRun($('#p-models'), loadModels));
  if (r.error || !Array.isArray(r.models)) throw new Error(r.error || '模型设置读取失败。');
  MODELHUB.data = r;
  if (!$('#modelDlg').open) $('#modelPreset').replaceChildren(...r.presets.map(p => new Option(p.name, p.id)));
  const tbody = $('#modelRows'); tbody.replaceChildren();
  for (const p of r.models){
    const tr = document.createElement('tr');
    const tags = [p.vision ? '文字 · 图片' : '文字'];
    if (p.id === r.default_chat) tags.push('默认聊天');
    if (p.id === r.default_image) tags.push('识图');
    tr.innerHTML = '<td><b>' + esc(p.name) + '</b><small>' + esc(p.model) + ' · ' + esc(p.base_url) + '</small></td><td>' + esc(tags.join(' · ')) +
      '<small>' + (p.key_set ? '密钥已配置' : '尚未配置密钥') + '</small></td><td><div class="row"></div></td>';
    const actions = tr.querySelector('.row');
    for (const [label, fn] of [['验证', () => modelVerify(p.id)], ['模型列表', () => modelReadList(p.source_id)], ['编辑', () => modelEditor(p.id)]]){
      const b = document.createElement('button'); b.className = 'ghost'; b.textContent = label; b.onclick = fn; actions.appendChild(b);
    }
    tbody.appendChild(tr);
  }
  if (!r.models.length) tbody.innerHTML = '<tr><td colspan="3" class="empty">尚未添加连接<div class="row"><button class="primary" onclick="modelEditor()">添加连接</button></div></td></tr>';
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
  if (!$('#modelRoleNote').dataset.busy) $('#modelRoleNote').textContent = '预设核对日期：' + r.checked_on;
}
function modelPresetChanged(){
  const p = MODELHUB.data?.presets.find(x => x.id === $('#modelPreset').value); if (!p) return;
  $('#modelName').value = p.name; $('#modelBase').value = p.base_url; $('#modelCode').value = p.model;
  $('#modelProtocol').value = p.protocol; $('#modelVision').checked = p.vision;
  $('#modelExtra').value = JSON.stringify(p.extra_body || {}, null, 2);
  $('#modelHeaders').value = ''; modelBaseCheck(true);
  $('#modelPresetHint').textContent = p.note + (p.model ? ' 默认型号：' + p.model : '');
  $('#modelVisionNote').textContent = '图片能力仅对应所选型号，更换型号后需要重新确认。';
}
function modelEditor(id){
  if (!MODELHUB.data || MODELHUB.busy) return;
  $('#modelSaveNote').textContent = ''; $('#modelKey').value = '';
  $('#modelEditID').value = id || ''; $('#modelPreset').disabled = !!id;
  const p = MODELHUB.data.models.find(x => x.id === id);
  $('#modelEditTitle').textContent = p ? '编辑连接 · ' + p.name : '添加连接';
  if (p){
    $('#modelPreset').value = p.preset; if (!$('#modelPreset').value) $('#modelPreset').value = 'custom';
    $('#modelName').value = p.name; $('#modelBase').value = p.base_url; $('#modelCode').value = p.model;
    $('#modelProtocol').value = p.protocol; $('#modelVision').checked = p.vision;
    $('#modelExtra').value = JSON.stringify(p.extra_body || {}, null, 2); $('#modelHeaders').value = '';
    $('#modelKey').placeholder = '留空则保留原密钥；填写后替换';
    $('#modelKeyNote').textContent = p.shared_source ? '此连接由多个型号共用，修改地址或密钥会同时影响这些型号。' : '密钥不回显，留空则保留。';
    $('#modelPresetHint').textContent = p.has_headers ? '已设置额外请求头，留空则保留；填写 JSON 对象则替换。' : '修改后请重新验证模型是否可用。';
    modelBaseCheck(true);
  } else {
    $('#modelPreset').value = 'openai'; modelPresetChanged();
    $('#modelKey').placeholder = '填写 API Key'; $('#modelKeyNote').textContent = '保存后不回显密钥。';
  }
  modelPane('basic'); $('#modelDlg').showModal();
}
// API 基础地址：自定义校验，不使用浏览器的校验气泡
function modelBaseCheck(quiet){
  const v = $('#modelBase').value.trim(), ok = !v || /^https?:\/\/[^\s/]+/i.test(v);
  const msg = ok ? '' : '地址需要以 http:// 或 https:// 开头，例如 https://api.example.com/v1';
  if (!quiet || ok){ $('#modelBaseErr').textContent = msg; $('#modelBase').classList.toggle('bad', !ok); }
  return ok;
}
async function modelSave(){
  if (MODELHUB.busy) return;
  if (!modelBaseCheck()){ modelPane('adv'); $('#modelBase').focus(); return; }
  let body;
  try {
    body = {id:$('#modelEditID').value, preset:$('#modelPreset').value, name:$('#modelName').value,
      key:$('#modelKey').value.trim(), base_url:$('#modelBase').value.trim(), protocol:$('#modelProtocol').value,
      model:$('#modelCode').value.trim(), vision:$('#modelVision').checked,
      extra_body:JSON.parse($('#modelExtra').value || '{}')};
    if ($('#modelHeaders').value.trim()) body.headers = JSON.parse($('#modelHeaders').value);
  } catch(e){ modelPane('adv'); $('#modelSaveNote').textContent = '其他参数或额外请求头不是有效的 JSON 对象。'; return; }
  MODELHUB.busy = true; $('#modelSaveNote').textContent = '';
  try {
    const r = await api('/api/models/save', body);
    if (!r.ok) throw new Error(r.error || '保存失败');
    $('#modelKey').value = ''; $('#modelDlg').close(); CHAT.options = null;
    toast('连接已保存\n现有会话尚未切换。可先验证模型，再在「用途分配」中应用。', 'ok');
  } catch(e){ $('#modelSaveNote').textContent = e.message + '。填写内容已保留。'; }
  finally { MODELHUB.busy = false; }
  if (!$('#modelDlg').open) await loadModels().catch(() => {});
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
async function modelVerify(id){
  if (MODELHUB.busy) return;
  MODELHUB.busy = true;
  try {
    const r = await api('/api/models/test', {id});
    infoDlg(r.ok ? '验证通过' : '验证未通过', `<p style="white-space:pre-wrap">${esc(r.message || r.error || '验证未完成，请稍后重试。')}</p>`);
  } catch(e){ infoDlg('验证未完成', `<p>${esc(e.message)}</p>`); }
  finally { MODELHUB.busy = false; }
}
async function modelReadList(source){
  const r = await api('/api/models/list?source=' + encodeURIComponent(source)).catch(e => ({error: e.message}));
  if (!r?.ok) return infoDlg('无法读取模型列表', `<p>${esc(r?.message || r?.error || '暂时无法读取，可以手动填写模型 ID。')}</p>`);
  const items = Array.isArray(r.models) ? r.models : [];
  infoDlg('服务商返回的模型', '<p class="hint">型号是否支持图片和工具调用，以服务商说明和实际验证为准。</p><div style="white-space:pre-wrap">' +
    esc(items.map(x => typeof x === 'string' ? x : x.id || x.name || '').filter(Boolean).join('\n') || '服务商未返回模型列表，可以手动填写模型 ID。') + '</div>');
}
$$('#modelTabs button').forEach(b => b.onclick = () => modelTab(b.dataset.mt2));
$$('#modelDlgTabs button').forEach(b => b.onclick = () => modelPane(b.dataset.mp));
$('#modelAdd').onclick = () => modelEditor();
$('#modelClose').onclick = () => { if (!MODELHUB.busy){ $('#modelDlg').close(); } };
$('#modelDlg').addEventListener('close', () => { $('#modelKey').value = ''; });
$('#modelRefresh').onclick = () => { apiFlush(); return loadModels().catch(e => toast('刷新失败\n' + e.message, 'err')); };
$('#modelPreset').onchange = () => { modelPresetChanged(); if ($('#modelPreset').value === 'custom') modelPane('adv'); };
$('#modelSave').onclick = modelSave;
$('#modelBase').addEventListener('blur', () => modelBaseCheck());
$('#modelBase').addEventListener('input', () => { if ($('#modelBase').classList.contains('bad')) modelBaseCheck(); });
$('#modelKey').addEventListener('paste', e => { const t = (e.clipboardData || window.clipboardData)?.getData('text'); if (t == null) return; e.preventDefault(); $('#modelKey').setRangeText(t.trim(), $('#modelKey').selectionStart, $('#modelKey').selectionEnd, 'end'); });
$('#modelCode').oninput = () => {
  const p = MODELHUB.data?.presets.find(x => x.id === $('#modelPreset').value);
  if ($('#modelCode').value !== p?.model){ $('#modelVision').checked = false; $('#modelVisionNote').textContent = '该型号的图片能力尚未确认，确认支持后再开启。'; }
};
$('#modelUseChat').onclick = () => modelAssign('chat', $('#modelDefaultChat').value);
$('#modelUseImage').onclick = () => modelAssign('image', $('#modelImage').value);
$('#modelApplyRules').onclick = async () => {
  if (MODELHUB.busy) return; MODELHUB.busy = true;
  try { const r = await api('/api/models/image-rules', {}); if (!r.ok) throw new Error(r.error || '识图规则未更新');
    toast('识图规则已更新\n下次识图将按新规则生成描述，旧的描述缓存不会混用。', 'ok');
  } catch(e){ toast(e.message, 'err'); } finally { MODELHUB.busy = false; }
};

if (curPage === 'models') skRun($('#p-models'), loadModels);
