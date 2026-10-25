const bridge = window.AstrBotPluginPage;
const MODAL_ANIM_MS = 200;
let refreshTimer = null;
let firstLoadDone = false;
let firstRenderDone = false;
// 最近一次状态数据：规则模态框从这里读当前规则（含实时命中数）
let lastStatus = null;
// 规则模态框草稿：打开时深拷贝自 lastStatus，保存时整单提交（replace 语义）
let rulesDraft = [];
let rulesSnapshot = '';
let rulesProviderId = null;
let editingRuleIndex = -1; // >=0 表示正在编辑某条已有规则（按草稿下标定位，未保存的新增规则无稳定 id）

async function fetchStatus() {
  try {
    // 调用插件后端 API，无需加插件名前缀
    const data = await bridge.apiGet('status');
    return data;
  } catch (err) {
    console.error('获取状态失败:', err);
    return null;
  }
}

// 监控页背景图：仅当配置了背景图时才注入并淡入；未配置则保持默认主题底色。
// 与 watchdoc / TTS Enhancer 同款实现：预加载图片、解码完成后再提升透明度，避免半图闪烁。
async function applyPageBackground() {
  try {
    const result = await bridge.apiGet('page-background');
    const data = result?.data || result;
    // 未配置背景图（data_url 为空）则不显示，保持默认主题底色
    if (!data || !data.data_url) return;
    const img = document.getElementById('page-bg-img');
    if (!img) return;
    const opacity = String(data.opacity ?? 0.2);
    const blur = Number(data.blur ?? 0);
    // 预加载图片，解码完成后再设置背景并提升透明度：CSS 中 #page-bg-img 默认 opacity 0
    // 且带 transition，此处赋值即触发从 0 到目标值的淡入；图片加载失败则保持透明不显示
    const preload = new Image();
    preload.onload = () => {
      img.style.backgroundImage = `url("${data.data_url}")`;
      img.style.filter = blur > 0 ? `blur(${blur}px)` : 'none';
      img.style.opacity = opacity;
    };
    preload.src = data.data_url;
  } catch (e) {
    console.warn('加载背景图失败:', e);
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[char]);
}

function formatMinutes(minutes) {
  const num = Number(minutes);
  const text = Number.isInteger(num) ? String(num) : num.toFixed(1);
  return `${text} 分钟`;
}

function renderPolicy(policy) {
  const el = document.getElementById('policy-hint');
  if (!el) return;

  if (!policy || policy.length === 0) {
    el.textContent = '';
    return;
  }

  const parts = policy.map((entry) => {
    const [failures, minutes] = entry;
    return `${failures} 次→${formatMinutes(minutes)}`;
  });
  el.textContent = `冷却阶梯: ${parts.join('，')}`;
}

function renderLoading(container) {
  container.innerHTML = `
    <div class="loading-box">
      <span class="spinner"></span>
      <span>加载中…</span>
    </div>`;
}

function renderVirtualModels(virtualModels, cooldownList, disabledList, failCounts, customRules) {
  const container = document.getElementById('virtual-models');
  if (!virtualModels || virtualModels.length === 0) {
    container.innerHTML = '<p>暂无虚拟模型配置</p>';
    return;
  }

  // 构建 cooldown 映射: provider_id -> remaining_secs
  const cooldownMap = {};
  if (cooldownList) {
    for (const item of cooldownList) {
      cooldownMap[item.id] = item.remaining_secs;
    }
  }

  const disabledSet = new Set(disabledList || []);
  const failMap = failCounts || {};
  const rulesMap = customRules || {};

  let html = '';
  for (const v of virtualModels) {
    const providerIds = v.provider_ids || [];
    const count = providerIds.length;

    html += `<div class="virtual-model-section">
      <div class="virtual-name">
        <span>${escapeHtml(v.name)}</span>
        <span class="provider-count">${count} 个 Provider</span>
      </div>
      <div class="provider-grid">`;

    if (count === 0) {
      html += `<p style="grid-column:1/-1; color:#888;">该虚拟模型未配置 Provider</p>`;
    } else {
      for (const pid of providerIds) {
        const remaining = cooldownMap[pid] || 0;
        const failures = failMap[pid] || 0;
        const ruleCount = (rulesMap[pid] || []).length;
        const isDisabled = disabledSet.has(pid);
        const isCooldown = remaining > 0;

        let statusClass = 'available';
        let statusText = '可用';
        if (isDisabled) {
          statusClass = 'disabled';
          statusText = '已禁用';
        } else if (isCooldown) {
          statusClass = 'cooldown';
          statusText = '冷却中';
        }

        // 剩余冷却时间与连续失败次数合并一行，避免卡片高度参差不齐
        const metaParts = [];
        if (isCooldown && !isDisabled) {
          metaParts.push('<span class="meta-cooldown">剩余 ' + remaining + ' 秒</span>');
        }
        if (failures > 0) {
          metaParts.push('<span class="meta-fail">连续失败 ' + failures + ' 次</span>');
        }
        if (ruleCount > 0) {
          metaParts.push('<span class="meta-rule">' + ruleCount + ' 条自定义规则</span>');
        }
        const metaHtml = metaParts.length > 0
          ? `<div class="provider-meta">${metaParts.join('<span class="meta-sep">·</span>')}</div>`
          : '';

        // 左信息列（名称/状态/元信息）与右按钮列相互独立：
        // 按钮三行不再把元信息挤到卡片底部，卡片高度取两列较大值
        html += `
          <div class="provider-card">
            <div class="card-main">
              <div class="name" title="${escapeHtml(pid)}">${escapeHtml(pid)}</div>
              <div class="status">
                <span class="dot ${statusClass}"></span>
                <span>${statusText}</span>
              </div>
              ${metaHtml}
            </div>
            <div class="card-actions">
              <button class="btn-outline toggle-btn" data-pid="${escapeHtml(pid)}" data-action="${isDisabled ? 'enable' : 'disable'}">
                ${isDisabled ? '解禁' : '禁用'}
              </button>
              <button class="btn-outline log-btn" data-pid="${escapeHtml(pid)}">日志</button>
              <button class="btn-outline rule-btn" data-pid="${escapeHtml(pid)}">规则</button>
            </div>
          </div>`;
      }
    }
    html += `</div></div>`;
  }
  // 首次渲染时分组依次淡入；之后每 30s 的自动刷新不再重放动画，避免干扰阅读
  container.classList.toggle('first-load', !firstRenderDone);
  firstRenderDone = true;
  container.innerHTML = html;

  for (const btn of container.querySelectorAll('.toggle-btn')) {
    btn.addEventListener('click', onToggleClick);
  }
  for (const btn of container.querySelectorAll('.log-btn')) {
    btn.addEventListener('click', onLogClick);
  }
  for (const btn of container.querySelectorAll('.rule-btn')) {
    btn.addEventListener('click', onRuleClick);
  }
}

// 插件页在 iframe 沙箱里 alert() 会被拦截，用自建模态框代替
// ---------- 模态框开合（带入场/退场动画） ----------
function openModalEl(el) {
  if (el._hideTimer) {
    clearTimeout(el._hideTimer);
    el._hideTimer = null;
  }
  el.hidden = false;
  void el.offsetWidth; // 强制回流，让过渡从初始态开始播放
  el.classList.add('is-open');
}

function closeModalEl(el) {
  el.classList.remove('is-open');
  if (el._hideTimer) clearTimeout(el._hideTimer);
  // 退场动画播完后再隐藏：hidden 会直接 display:none，看不到退场
  el._hideTimer = setTimeout(() => {
    el.hidden = true;
    el._hideTimer = null;
  }, MODAL_ANIM_MS);
}

function showModal(message) {
  document.getElementById('modal-message').textContent = message;
  openModalEl(document.getElementById('modal'));
}

function closeModal() {
  closeModalEl(document.getElementById('modal'));
}

function closeLogModal() {
  closeModalEl(document.getElementById('log-modal'));
}

// ---------- 自定义规则模态框 ----------
// 动作下拉框宽度「顶着」当前选项文字：用同字体隐藏 span 实测文本宽，再加下拉箭头占用。
// 短选项（冷却/禁用）自动收窄，省下的宽度让给匹配内容输入框；长选项（冷却到次日 0 点）
// 自动加宽到刚好容纳，不会被右侧下拉按钮截断
const ACTION_ARROW_ALLOWANCE = 46; // 左内边距 8 + 边框 2 + 右下拉箭头与间距 + 余量
const actionMeasurer = document.createElement('span');
actionMeasurer.style.cssText = 'position:absolute;top:-9999px;visibility:hidden;white-space:nowrap;font-size:13px;';
document.body.appendChild(actionMeasurer);

function describeAction(rule) {
  if (rule.action === 'disable') return '禁用';
  if (rule.action === 'until_midnight') {
    const tz = rule.tz;
    return tz ? `冷却到次日 0 点（${tz}）` : '冷却到次日 0 点';
  }
  return `冷却 ${formatMinutes(rule.cooldown_minutes)}`;
}

function parseOffsetHours(num) {
  // 与服务端 _parse_offset_hours 对齐：支持 +8 / -5 / 8 / 5.5 / 05:30（冒号=分钟）
  let sign = 1;
  let s = num;
  if (s[0] === '+') s = s.slice(1);
  else if (s[0] === '-') { sign = -1; s = s.slice(1); }
  let hours;
  if (s.indexOf(':') !== -1) {
    const parts = s.split(':', 2);
    hours = parseInt(parts[0], 10) + parseInt(parts[1], 10) / 60;
  } else {
    hours = parseFloat(s);
  }
  return sign * hours;
}

function validateTz(tz) {
  // 与服务端 _parse_midnight_tz 对齐：local / UTC / GMT / UTC±N / GMT±N / 纯数字偏移（-24~24）
  const v = (tz || '').trim();
  if (v === '') return null;
  const low = v.toLowerCase();
  if (low === 'local' || low === 'utc' || low === 'gmt') return null;
  // UTC/GMT 前缀 + 带符号数字（可含 :mm 或 .5）
  const m = /^(?:utc|gmt)\s*([+-]?\d+(?::\d+)?(?:\.\d+)?)$/.exec(low);
  let offset;
  if (m) {
    offset = parseOffsetHours(m[1]);
  } else {
    // 容错：单独写 "+8" / "-5" / "8" / "5.5"
    offset = Number(v);
  }
  if (!Number.isFinite(offset)) {
    return '时区格式无效（应为 local / UTC / GMT / UTC±N，或偏移小时数如 -8、+9、+5.5，范围 -24~24）';
  }
  if (offset <= -24 || offset >= 24) {
    return '时区偏移超出范围（应在 -24 ~ 24 之间）';
  }
  return null;
}

function isRulesDirty() {
  return JSON.stringify(rulesDraft) !== rulesSnapshot;
}

function openRulesModal(providerId) {
  rulesProviderId = providerId;
  const stored = (lastStatus && lastStatus.custom_rules && lastStatus.custom_rules[providerId]) || [];
  // 深拷贝：草稿的增删不能直接写进 lastStatus，否则未保存也会被当成已保存
  rulesDraft = stored.map((rule) => ({ ...rule }));
  rulesSnapshot = JSON.stringify(rulesDraft);
  applyRuleFormToAddMode();
  openModalEl(document.getElementById('rules-modal'));
}

// 把表单切回「新增」状态：清空输入、次数默认 1、动作回到冷却、按钮文案恢复
function applyRuleFormToAddMode() {
  editingRuleIndex = -1;
  document.getElementById('rules-title').textContent = `自定义规则：${rulesProviderId || ''}`;
  document.getElementById('rule-pattern').value = '';
  document.getElementById('rule-threshold').value = '1';
  document.getElementById('rule-action').value = 'cooldown';
  document.getElementById('rule-minutes').value = '';
  document.getElementById('rule-tz').value = '';
  const addBtn = document.getElementById('rule-add-btn');
  addBtn.textContent = '添加';
  const cancelBtn = document.getElementById('rule-cancel-btn');
  cancelBtn.textContent = '清空';
  syncMinutesVisibility();
  syncTzVisibility();
  syncActionWidth();
  renderRulesList();
}

function closeRulesModal(saved) {
  // 丢弃未保存修改时明确告知，避免用户误以为已生效
  if (!saved && isRulesDirty()) {
    showModal('已丢弃未保存的规则修改');
  }
  closeModalEl(document.getElementById('rules-modal'));
}

function renderRulesList() {
  const list = document.getElementById('rules-list');
  document.getElementById('rules-dirty').hidden = !isRulesDirty();

  if (rulesDraft.length === 0) {
    list.innerHTML = '<p class="rules-empty">暂无规则，可在下方添加</p>';
    return;
  }

  let html = '';
  for (let i = 0; i < rulesDraft.length; i++) {
    const rule = rulesDraft[i];
    const hits = rule.hits || 0;
    // 达到阈值即下一次匹配命中时触发，高亮提醒
    const hot = hits >= rule.threshold;
    const editing = i === editingRuleIndex;
    html += `<div class="rule-item${editing ? ' editing' : ''}">
      <div class="rule-info">
        <div class="rule-pattern">${escapeHtml(rule.pattern)}</div>
        <div class="rule-condition">连续 ${rule.threshold} 次 → ${escapeHtml(describeAction(rule))}</div>
      </div>
      <span class="rule-hits${hot ? ' hot' : ''}" title="已连续命中次数 / 触发阈值">${hits}/${rule.threshold}</span>
      <button class="btn-outline rule-edit-btn" data-rule-index="${i}">编辑</button>
      <button class="btn-outline rule-delete-btn" data-rule-index="${i}">删除</button>
    </div>`;
  }
  list.innerHTML = html;
  for (const btn of list.querySelectorAll('.rule-edit-btn')) {
    btn.addEventListener('click', onRuleEditClick);
  }
  for (const btn of list.querySelectorAll('.rule-delete-btn')) {
    btn.addEventListener('click', onRuleDeleteClick);
  }
}

function syncMinutesVisibility() {
  // 冷却分钟数仅对「冷却」动作有意义，其余动作隐藏避免误填
  document.getElementById('rule-minutes').hidden = document.getElementById('rule-action').value !== 'cooldown';
}

function syncTzVisibility() {
  // 时区仅对「冷却到次日 0 点」动作有意义，其余动作隐藏避免误填
  const showTz = document.getElementById('rule-action').value === 'until_midnight';
  document.getElementById('rule-tz').hidden = !showTz;
  document.getElementById('rule-tz-hint').hidden = !showTz;
}

function syncActionWidth() {
  const sel = document.getElementById('rule-action');
  const option = sel.options[sel.selectedIndex];
  if (!option) return;
  actionMeasurer.textContent = option.text;
  sel.style.width = `${Math.ceil(actionMeasurer.offsetWidth) + ACTION_ARROW_ALLOWANCE}px`;
}

function onRuleClick(event) {
  openRulesModal(event.currentTarget.dataset.pid);
}

function onRuleDeleteClick(event) {
  const index = Number(event.currentTarget.dataset.ruleIndex);
  if (Number.isInteger(index) && index >= 0 && index < rulesDraft.length) {
    rulesDraft.splice(index, 1);
    // 正编辑的条目被删：退回新增态，避免悬空下标；其后的条目下标前移
    if (editingRuleIndex === index) {
      applyRuleFormToAddMode();
    } else if (editingRuleIndex > index) {
      editingRuleIndex -= 1;
    }
    renderRulesList();
  }
}

function onRuleEditClick(event) {
  const index = Number(event.currentTarget.dataset.ruleIndex);
  if (!(Number.isInteger(index) && index >= 0 && index < rulesDraft.length)) return;
  const rule = rulesDraft[index];
  editingRuleIndex = index;
  document.getElementById('rules-title').textContent = `编辑规则：${rulesProviderId || ''}`;
  document.getElementById('rule-pattern').value = rule.pattern || '';
  document.getElementById('rule-threshold').value = String(rule.threshold != null ? rule.threshold : 1);
  document.getElementById('rule-action').value = rule.action || 'cooldown';
  document.getElementById('rule-minutes').value =
    rule.action === 'cooldown' && rule.cooldown_minutes != null ? String(rule.cooldown_minutes) : '';
  document.getElementById('rule-tz').value = rule.action === 'until_midnight' ? (rule.tz || '') : '';
  document.getElementById('rule-add-btn').textContent = '更新';
  document.getElementById('rule-cancel-btn').textContent = '放弃';
  syncMinutesVisibility();
  syncTzVisibility();
  syncActionWidth();
  renderRulesList(); // 让被编辑项高亮
  document.getElementById('rule-pattern').focus();
}

function onRuleCancelClick() {
  // 编辑态=放弃编辑；新增态=清空表单（文案与两态按钮约定一致）
  if (editingRuleIndex >= 0) {
    applyRuleFormToAddMode();
  } else {
    document.getElementById('rule-pattern').value = '';
    document.getElementById('rule-threshold').value = '1';
    document.getElementById('rule-action').value = 'cooldown';
    document.getElementById('rule-minutes').value = '';
    document.getElementById('rule-tz').value = '';
    syncMinutesVisibility();
    syncTzVisibility();
    syncActionWidth();
  }
}

function onRuleAddClick(event) {
  event.preventDefault();
  const pattern = document.getElementById('rule-pattern').value.trim();
  const thresholdRaw = document.getElementById('rule-threshold').value.trim();
  const action = document.getElementById('rule-action').value;
  const minutesRaw = document.getElementById('rule-minutes').value.trim();
  const tzRaw = document.getElementById('rule-tz').value.trim();

  // 前端校验与服务端 validate_rules 保持一致，消息同样用中文
  if (!pattern) {
    showModal('匹配内容不能为空');
    return;
  }
  if (pattern.length > 100) {
    showModal('匹配内容不能超过 100 个字符');
    return;
  }
  const threshold = Number(thresholdRaw);
  if (!thresholdRaw || !Number.isInteger(threshold) || threshold < 1) {
    showModal('连续次数必须是大于 0 的整数');
    return;
  }
  let cooldownMinutes = null;
  if (action === 'cooldown') {
    cooldownMinutes = Number(minutesRaw);
    if (!minutesRaw || !(cooldownMinutes > 0)) {
      showModal('冷却分钟数必须是大于 0 的数字');
      return;
    }
  }
  let tz = '';
  if (action === 'until_midnight' && tzRaw !== '') {
    const tzErr = validateTz(tzRaw);
    if (tzErr) {
      showModal(tzErr);
      return;
    }
    tz = tzRaw;
  }

  const rule = { pattern, threshold, action };
  if (action === 'cooldown') rule.cooldown_minutes = cooldownMinutes;
  if (action === 'until_midnight') rule.tz = tz;

  if (editingRuleIndex >= 0) {
    // 编辑：就地替换原规则，保留其在列表中的顺序与稳定 id；
    // 否则服务端会重新生成 id、按旧 id 清掉的命中计数全部清零（实测「改个错别字变 0/3」）
    rulesDraft[editingRuleIndex] = { ...rule, id: rulesDraft[editingRuleIndex].id };
  } else {
    rulesDraft.push(rule);
  }
  applyRuleFormToAddMode();
  document.getElementById('rule-pattern').focus();
}

async function onRuleSaveClick() {
  if (!rulesProviderId) return;
  const btn = document.getElementById('rules-save-btn');
  btn.disabled = true;
  try {
    const data = await bridge.apiPost('rules/set', {
      provider_id: rulesProviderId,
      rules: rulesDraft,
    });
    if (data && data.ok) {
      // 用服务端返回的归一化规则（含新 id 与实时命中数）更新本地缓存
      if (lastStatus) {
        lastStatus.custom_rules = lastStatus.custom_rules || {};
        lastStatus.custom_rules[rulesProviderId] = data.rules || [];
      }
      closeRulesModal(true);
      await refreshDashboard();
    } else {
      showModal(`保存失败: ${(data && data.error) || '未知错误'}`);
    }
  } catch (err) {
    console.error('保存自定义规则失败:', err);
    showModal(`保存失败: ${err && err.message ? err.message : err}`);
  } finally {
    btn.disabled = false;
  }
}

function formatLogTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString();
}

function openLogModal(providerId) {
  document.getElementById('log-title').textContent = `调用日志：${providerId}`;
  document.getElementById('log-list').innerHTML = `
    <div class="loading-box">
      <span class="spinner"></span>
      <span>加载中…</span>
    </div>`;
  openModalEl(document.getElementById('log-modal'));
}

function fillLogModal(logs) {
  const list = document.getElementById('log-list');

  if (!logs || logs.length === 0) {
    list.innerHTML = '<p class="log-empty">暂无调用记录</p>';
    return;
  }

  let html = '';
  for (const entry of logs) {
    const ok = !!entry.success;
    html += `<div class="log-item">
      <div class="log-head">
        <span class="log-time">${escapeHtml(formatLogTime(entry.time))}</span>
        <span class="log-badge ${ok ? 'ok' : 'fail'}">${ok ? '成功' : '失败'}</span>
      </div>
      ${entry.reason ? `<div class="log-reason">${escapeHtml(entry.reason)}</div>` : ''}
    </div>`;
  }
  list.innerHTML = html;
}

async function onLogClick(event) {
  const btn = event.currentTarget;
  const providerId = btn.dataset.pid;

  btn.disabled = true;
  openLogModal(providerId); // 先以加载态打开，避免空窗
  try {
    const data = await bridge.apiGet('logs', { provider_id: providerId });
    fillLogModal((data && data.logs) || []);
  } catch (err) {
    console.error('获取调用日志失败:', err);
    closeLogModal();
    showModal(`获取调用日志失败: ${err && err.message ? err.message : err}`);
  } finally {
    btn.disabled = false;
  }
}

async function onToggleClick(event) {
  const btn = event.currentTarget;
  const providerId = btn.dataset.pid;
  const disabled = btn.dataset.action === 'disable';

  btn.disabled = true;
  try {
    await bridge.apiPost('provider/set_disabled', { provider_id: providerId, disabled });
    await refreshDashboard();
  } catch (err) {
    console.error('切换禁用状态失败:', err);
    btn.disabled = false;
    showModal(`操作失败: ${err && err.message ? err.message : err}`);
  }
}

async function refreshDashboard() {
  const container = document.getElementById('virtual-models');
  // 仅首次加载显示占位；之后的定时刷新直接换内容，避免每 30s 闪一下
  if (!firstLoadDone) {
    renderLoading(container);
  }

  const data = await fetchStatus();
  if (!data) {
    container.innerHTML = '<p>加载失败，请重试</p>';
    return;
  }
  firstLoadDone = true;
  lastStatus = data;

  renderPolicy(data.cooldown_policy);
  renderVirtualModels(
    data.virtual_models,
    data.cooldown_list,
    data.disabled_list,
    data.fail_counts,
    data.custom_rules
  );

  const now = new Date();
  document.getElementById('last-update').textContent = `最后更新: ${now.toLocaleTimeString()}`;
}

// 初始化
async function init() {
  await bridge.ready();
  await refreshDashboard();
  // 不阻塞首屏：背景图可能数 MB，await 它会让面板一直白着等图。
  // 去掉 await 后，图片加载完由内部 preload.onload 异步淡入，不影响主内容渲染。
  applyPageBackground();

  document.getElementById('refresh-btn').addEventListener('click', refreshDashboard);

  document.getElementById('modal-ok').addEventListener('click', closeModal);
  document.getElementById('modal').addEventListener('click', (event) => {
    if (event.target.id === 'modal') closeModal();
  });

  document.getElementById('log-close').addEventListener('click', closeLogModal);
  document.getElementById('log-modal').addEventListener('click', (event) => {
    if (event.target.id === 'log-modal') closeLogModal();
  });

  document.getElementById('rules-close').addEventListener('click', () => closeRulesModal(false));
  document.getElementById('rules-save-btn').addEventListener('click', onRuleSaveClick);
  document.getElementById('rule-form').addEventListener('submit', onRuleAddClick);
  document.getElementById('rule-cancel-btn').addEventListener('click', onRuleCancelClick);
  document.getElementById('rule-action').addEventListener('change', () => {
    syncMinutesVisibility();
    syncTzVisibility();
    syncActionWidth();
  });
  document.getElementById('rules-modal').addEventListener('click', (event) => {
    if (event.target.id === 'rules-modal') closeRulesModal(false);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeModal();
      closeLogModal();
      closeRulesModal(false);
    }
  });

  // 每30秒自动刷新
  refreshTimer = setInterval(refreshDashboard, 30000);

  window.addEventListener('beforeunload', () => {
    if (refreshTimer) clearInterval(refreshTimer);
  });
}

init();
