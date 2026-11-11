/* Model connections use the same controls and API helpers as the main console. */
const MODELHUB = {data:null, busy:false};
function modelOptions(select, models, value){
  select.replaceChildren(...models.map(p => new Option(p.name + ' / ' + p.model, p.id)));
  if (models.some(p => p.id === value)) select.value = value;
}
async function loadModels(){
  if (MODELHUB.busy) return;
  try {
    const r = await api('/api/models');
    if (r.need_login && needLogin(r, $('#modelNeedLogin'), loadModels)) return;
    if (r.error || !Array.isArray(r.models)) throw new Error(r.error || '模型设置读取失败');
    MODELHUB.data = r;
    $('#modelPreset').replaceChildren(...r.presets.map(p => new Option(p.name,p.id)));
    const tbody = $('#modelRows'); tbody.replaceChildren();
    for (const p of r.models){
      const tr = document.createElement('tr');
      let use = p.vision ? '文字 · 图片' : '文字';
      if (p.id === r.default_chat) use += ' · 默认聊天';
      if (p.id === r.default_image) use += ' · 识图';
      tr.innerHTML = '<td><b>' + esc(p.name) + '</b><small>' + esc(p.model) + '</small><small>' +
        esc(p.base_url) + '</small></td><td>' + esc(use) +
        '<small>' + (p.key_set ? '密钥已配置' : '未配置密钥') + '</small></td><td><div class="row"></div></td>';
      const actions = tr.querySelector('.row');
      for (const pair of [['编辑',()=>modelEditor(p.id)],['验证模型',()=>modelVerify(p.id)],['读取模型列表',()=>modelReadList(p.source_id)]]){
        const b = document.createElement('button'); b.textContent = pair[0]; b.onclick = pair[1]; actions.appendChild(b);
      }
      tbody.appendChild(tr);
    }
    if (!r.models.length) tbody.innerHTML = '<tr><td colspan="3">还没有模型，点“添加连接”开始。</td></tr>';
    const enabled = r.models.filter(p => p.enabled);
    modelOptions($('#modelDefaultChat'),enabled,r.default_chat);
    modelOptions($('#modelImage'),enabled.filter(p => p.vision),r.default_image);
    $('#modelUseChat').disabled = !enabled.length;
    $('#modelUseImage').disabled = !enabled.some(p => p.vision);
    const sessions = $('#modelSessions'); sessions.replaceChildren();
    for (const s of r.sessions){
      const tr = document.createElement('tr');
      tr.innerHTML = '<td>' + esc(s.label) + (s.inherited ? '<small>当前跟随默认模型</small>' : '<small>已单独指定</small>') + '</td><td></td><td></td>';
      const select = document.createElement('select');
      select.setAttribute('aria-label',s.label + '的聊天模型'); modelOptions(select,enabled,s.provider);
      tr.children[1].appendChild(select);
      const b = document.createElement('button'); b.textContent = '应用'; b.disabled = !enabled.length;
      b.onclick = () => modelAssign('session',select.value,s.umo);
      tr.children[2].appendChild(b); sessions.appendChild(tr); enhanceSelect(select);
    }
    $('#modelRoleNote').textContent = '预设核对日期：' + r.checked_on;
  } catch(e){ $('#modelRows').innerHTML = '<tr><td colspan="3">' + esc(e.message) + '，可点刷新重试。</td></tr>'; }
}
function modelPresetChanged(){
  const p = MODELHUB.data?.presets.find(x => x.id === $('#modelPreset').value); if (!p) return;
  $('#modelName').value = p.name; $('#modelBase').value = p.base_url; $('#modelCode').value = p.model;
  $('#modelProtocol').value = p.protocol; $('#modelVision').checked = p.vision;
  $('#modelExtra').value = JSON.stringify(p.extra_body || {},null,2);
  $('#modelHeaders').value = ''; $('#modelAdvanced').open = p.id === 'custom';
  $('#modelPresetHint').textContent = p.note + (p.model ? ' 默认型号：' + p.model : '');
  $('#modelVisionNote').textContent = '图片能力仅对应所选型号；修改型号后需要重新确认。';
}
function modelEditor(id){
  if (!MODELHUB.data || MODELHUB.busy) return;
  $('#modelEditor').hidden = false; $('#modelSaveNote').textContent = ''; $('#modelKey').value = '';
  $('#modelEditID').value = id || ''; $('#modelPreset').disabled = !!id;
  const p = MODELHUB.data.models.find(x => x.id === id);
  $('#modelEditTitle').textContent = p ? '编辑模型连接' : '添加模型连接';
  if (p){
    $('#modelPreset').value = p.preset; if (!$('#modelPreset').value) $('#modelPreset').value = 'custom';
    $('#modelName').value = p.name; $('#modelBase').value = p.base_url; $('#modelCode').value = p.model;
    $('#modelProtocol').value = p.protocol; $('#modelVision').checked = p.vision;
    $('#modelExtra').value = JSON.stringify(p.extra_body || {},null,2); $('#modelHeaders').value = '';
    $('#modelAdvanced').open = true;
    $('#modelKey').placeholder = '留空保留原密钥；填写新密钥则替换';
    $('#modelKeyNote').textContent = p.shared_source ? '此连接由多个型号共用；修改地址或密钥会同时影响它们。' : '密钥不回显，留空保留。';
    $('#modelPresetHint').textContent = p.has_headers ? '已有额外请求头，留空保留；填写JSON对象则替换。' : '修改后需重新验证模型可用性。';
  } else {
    $('#modelPreset').value = 'openai'; modelPresetChanged();
    $('#modelKey').placeholder = '填写API Key'; $('#modelKeyNote').textContent = '保存后不回显密钥。';
  }
  $('#modelEditor').scrollIntoView({block:'nearest'}); $('#modelKey').focus();
}
async function modelSave(){
  if (MODELHUB.busy) return;
  let body;
  try {
    body = {id:$('#modelEditID').value,preset:$('#modelPreset').value,name:$('#modelName').value,
      key:$('#modelKey').value,base_url:$('#modelBase').value,protocol:$('#modelProtocol').value,
      model:$('#modelCode').value,vision:$('#modelVision').checked,
      extra_body:JSON.parse($('#modelExtra').value || '{}')};
    if ($('#modelHeaders').value.trim()) body.headers = JSON.parse($('#modelHeaders').value);
  } catch(e){ $('#modelSaveNote').textContent = 'JSON格式不正确，请检查额外参数和请求头'; return; }
  MODELHUB.busy = true; $('#modelEditor').inert = true; $('#modelSave').disabled = true; $('#modelSaveNote').textContent = '正在保存…';
  try {
    const r = await api('/api/models/save',body);
    if (!r.ok) throw new Error(r.error || '保存失败');
    $('#modelKey').value = ''; $('#modelEditor').hidden = true; CHAT.options = null;
    toast('连接已保存\n尚未切换现有会话，可先验证模型，再在用途分配中应用。','ok');
  } catch(e){ $('#modelSaveNote').textContent = e.message + '；输入已保留'; }
  finally { MODELHUB.busy = false; $('#modelEditor').inert = false; $('#modelSave').disabled = false; }
  if ($('#modelEditor').hidden) await loadModels();
}
async function modelAssign(role,provider,umo){
  if (MODELHUB.busy || !provider) return;
  MODELHUB.busy = true; $('#modelRoleNote').textContent = '正在应用…';
  try {
    const r = await api('/api/models/assign',{role,provider,umo});
    if (!r.ok) throw new Error(r.error || '模型分配失败');
    CHAT.options = null; toast('模型分配已更新','ok');
  } catch(e){ toast(e.message,'err'); }
  finally { MODELHUB.busy = false; }
  await loadModels();
}
async function modelVerify(id){
  if (MODELHUB.busy) return;
  MODELHUB.busy = true; $('#modelRoleNote').textContent = '正在验证，可能需要几十秒…';
  try {
    const r = await api('/api/models/test',{id});
    toast(r.message || r.error || '验证未完成', r.ok ? 'ok' : 'err');
  } catch(e){ toast('验证未完成，请稍后重试','err'); }
  finally { MODELHUB.busy = false; $('#modelRoleNote').textContent = '验证结束'; }
}
async function modelReadList(source){
  const r = await api('/api/models/list?source=' + encodeURIComponent(source)).catch(()=>null);
  if (!r?.ok) return toast(r?.message || r?.error || '暂时无法读取，可手动填写模型ID','err');
  const items = Array.isArray(r.models) ? r.models : [];
  infoDlg('服务商返回的模型', '<p class="hint">型号是否支持图片和工具，需要以服务商说明及实际验证为准。</p><div style="white-space:pre-wrap">' +
    esc(items.map(x => typeof x === 'string' ? x : x.id || x.name || '').filter(Boolean).join('\n') || '没有返回模型列表，可以手动填写') + '</div>');
}
$('#modelAdd').onclick = () => modelEditor();
$('#modelClose').onclick = () => { if (!MODELHUB.busy){ $('#modelEditor').hidden = true; $('#modelKey').value = ''; } };
$('#modelRefresh').onclick = loadModels; $('#modelPreset').onchange = modelPresetChanged; $('#modelSave').onclick = modelSave;
$('#modelCode').oninput = () => {
  const p = MODELHUB.data?.presets.find(x => x.id === $('#modelPreset').value);
  if ($('#modelCode').value !== p?.model){ $('#modelVision').checked = false; $('#modelVisionNote').textContent = '该型号的图片能力未确认；确认支持后可勾选。'; }
};
$('#modelUseChat').onclick = () => modelAssign('chat',$('#modelDefaultChat').value);
$('#modelUseImage').onclick = () => modelAssign('image',$('#modelImage').value);
$('#modelApplyRules').onclick = async () => {
  if (MODELHUB.busy) return; MODELHUB.busy = true;
  try { const r = await api('/api/models/image-rules',{}); if (!r.ok) throw new Error(r.error || '未应用成功');
    toast('识图规则已更新\n下次识图会按新规则生成描述，旧描述缓存不会混用。','ok');
  } catch(e){ toast(e.message,'err'); } finally { MODELHUB.busy = false; }
};

if (curPage === 'models') loadModels();
