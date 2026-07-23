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

function renderVirtualModels(virtualModels, cooldownList) {
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

  let html = '';
  for (const v of virtualModels) {
    const providerIds = v.provider_ids || [];
    const count = providerIds.length;

    html += `<div class="virtual-model-section">
      <div class="virtual-name">
        <span>${v.name}</span>
        <span class="provider-count">${count} 个 Provider</span>
      </div>
      <div class="provider-grid">`;

    if (count === 0) {
      html += `<p style="grid-column:1/-1; color:#888;">该虚拟模型未配置 Provider</p>`;
    } else {
      for (const pid of providerIds) {
        const remaining = cooldownMap[pid] || 0;
        const isCooldown = remaining > 0;
        const statusClass = isCooldown ? 'cooldown' : 'available';
        const statusText = isCooldown ? '冷却中' : '可用';

        html += `
          <div class="provider-card">
            <div class="name">${pid}</div>
            <div class="status">
              <span class="dot ${statusClass}"></span>
              <span>${statusText}</span>
            </div>`;
        if (isCooldown) {
          html += `<div class="cooldown-timer">⏱ 剩余 ${remaining} 秒</div>`;
        }
        html += `</div>`;
      }
    }
    html += `</div></div>`;
  }
  container.innerHTML = html;
}

async function refreshDashboard() {
  const data = await fetchStatus();
  if (!data) {
    document.getElementById('virtual-models').innerHTML = '<p>加载失败，请重试</p>';
    return;
  }

  renderVirtualModels(data.virtual_models, data.cooldown_list);

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
