const bridge = window.AstrBotPluginPage;
let refreshTimer = null;

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

function renderVirtualModels(virtualModels, cooldownList, disabledList, failCounts) {
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

        html += `
          <div class="provider-card">
            <div class="name" title="${escapeHtml(pid)}">${escapeHtml(pid)}</div>
            <div class="status-row">
              <div class="status">
                <span class="dot ${statusClass}"></span>
                <span>${statusText}</span>
              </div>
              <button class="toggle-btn" data-pid="${escapeHtml(pid)}" data-action="${isDisabled ? 'enable' : 'disable'}">
                ${isDisabled ? '解禁' : '禁用'}
              </button>
            </div>`;

        if (isCooldown && !isDisabled) {
          html += `<div class="cooldown-timer">⏱ 剩余 ${remaining} 秒</div>`;
        }
        if (failures > 0) {
          html += `<div class="fail-count">连续失败 ${failures} 次</div>`;
        }

        html += `</div>`;
      }
    }
    html += `</div></div>`;
  }
  container.innerHTML = html;

  for (const btn of container.querySelectorAll('.toggle-btn')) {
    btn.addEventListener('click', onToggleClick);
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
    alert(`操作失败: ${err && err.message ? err.message : err}`);
  }
}

async function refreshDashboard() {
  const data = await fetchStatus();
  if (!data) {
    document.getElementById('virtual-models').innerHTML = '<p>加载失败，请重试</p>';
    return;
  }

  renderPolicy(data.cooldown_policy);
  renderVirtualModels(data.virtual_models, data.cooldown_list, data.disabled_list, data.fail_counts);

  const now = new Date();
  document.getElementById('last-update').textContent = `最后更新: ${now.toLocaleTimeString()}`;
}

// 初始化
async function init() {
  await bridge.ready();
  await refreshDashboard();

  document.getElementById('refresh-btn').addEventListener('click', refreshDashboard);

  // 每30秒自动刷新
  refreshTimer = setInterval(refreshDashboard, 30000);

  window.addEventListener('beforeunload', () => {
    if (refreshTimer) clearInterval(refreshTimer);
  });
}

init();
