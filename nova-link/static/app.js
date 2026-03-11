// Nova-Link — PWA Dashboard for Nova-Core
const API = window.location.origin;
let currentPage = 'dashboard';
let ws = null;

// --- Navigation ---
function switchPage(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(`page-${page}`).classList.add('active');
  document.querySelector(`[data-page="${page}"]`).classList.add('active');
  currentPage = page;

  // Load data for the page
  if (page === 'dashboard') loadDashboard();
  else if (page === 'chat') loadChatHistory();
  else if (page === 'reports') loadReports();
  else if (page === 'controls') loadServices();
}

// --- Toast ---
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}

// --- API helper ---
async function api(path, opts = {}) {
  try {
    const res = await fetch(`${API}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (e) {
    console.error(`API error: ${path}`, e);
    toast(`Error: ${e.message}`);
    return null;
  }
}

// --- Dashboard ---
async function loadDashboard() {
  const data = await api('/api/dashboard');
  if (!data) return;

  const hb = data.heartbeat;
  const isHealthy = hb.status === 'HEALTHY';

  // Status header
  document.getElementById('status-dot').className =
    `status-dot ${isHealthy ? 'healthy' : 'unhealthy'}`;
  document.getElementById('status-text').textContent =
    isHealthy ? 'HEALTHY' : hb.status;

  // Heartbeat card
  document.getElementById('hb-checks').textContent =
    `${hb.checks_passed}/${hb.checks_total}`;
  document.getElementById('hb-time').textContent =
    hb.last_check ? timeAgo(hb.last_check) : 'never';

  // Tasks card
  const t = data.tasks;
  document.getElementById('tasks-pending').textContent = t.pending;
  document.getElementById('tasks-done').textContent = t.done;
  document.getElementById('tasks-failed').textContent = t.failed;

  // Services
  const svcEl = document.getElementById('services-list');
  svcEl.innerHTML = '';
  for (const [name, status] of Object.entries(data.services)) {
    const shortName = name.replace('novacore-', '');
    const isActive = status === 'active';
    svcEl.innerHTML += `
      <div class="service-item">
        <span class="service-name">${shortName}</span>
        <span class="service-badge ${isActive ? 'active' : 'inactive'}">${status}</span>
      </div>`;
  }

  // Goals
  const goalsEl = document.getElementById('goals-list');
  if (data.goals && data.goals.length > 0) {
    goalsEl.innerHTML = data.goals.map(g =>
      `<div class="check-item">
        <span class="check-icon">🎯</span>
        <span class="check-name">${g.text || g.title || 'Goal'}</span>
        <span class="check-detail">${g.priority || g.status || ''}</span>
      </div>`
    ).join('');
  } else {
    goalsEl.innerHTML = '<div class="check-item"><span style="color:var(--text-dim)">No active goals</span></div>';
  }

  // Research/Planning
  document.getElementById('last-research').textContent =
    data.last_research ? data.last_research.split('] ')[1] || data.last_research : 'No research yet';
  document.getElementById('last-planning').textContent =
    data.last_planning ? data.last_planning.split('] ')[1] || data.last_planning : 'No planning yet';
}

// --- Health checks detail ---
async function loadHealthChecks() {
  const data = await api('/api/heartbeat');
  if (!data) return;

  const el = document.getElementById('health-checks-detail');
  if (data.checks && data.checks.length > 0) {
    el.innerHTML = data.checks.map(c =>
      `<div class="check-item">
        <span class="check-icon">${c.ok ? '✅' : '❌'}</span>
        <span class="check-name">${c.name}</span>
        <span class="check-detail">${c.detail}</span>
      </div>`
    ).join('');
  }
  el.closest('.card').style.display = 'block';
}

// --- Chat ---
const chatMessages = [];
let chatHistoryLoaded = false;

async function loadChatHistory() {
  if (chatHistoryLoaded) return;
  const data = await api('/api/chat/history');
  if (!data || !data.messages) return;
  chatMessages.length = 0;
  for (const m of data.messages) {
    if (m.role === 'user' || m.role === 'assistant') {
      chatMessages.push({ role: m.role, content: m.content });
    }
  }
  chatHistoryLoaded = true;
  renderChat();
}

async function sendChat() {
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg) return;

  input.value = '';
  chatMessages.push({ role: 'user', content: msg });
  renderChat();

  // Show typing indicator
  chatMessages.push({ role: 'typing', content: '' });
  renderChat();

  const data = await api('/api/chat', {
    method: 'POST',
    body: JSON.stringify({ message: msg }),
  });

  // Remove typing indicator
  chatMessages.pop();

  if (data && data.response) {
    chatMessages.push({ role: 'assistant', content: data.response });
  } else {
    chatMessages.push({ role: 'assistant', content: 'No response received.' });
  }
  renderChat();
}

function renderChat() {
  const el = document.getElementById('chat-messages');
  el.innerHTML = chatMessages.map(m => {
    if (m.role === 'typing') {
      return '<div class="chat-bubble assistant"><span class="spinner"></span> Thinking...</div>';
    }
    return `<div class="chat-bubble ${m.role}">${escapeHtml(m.content)}</div>`;
  }).join('');
  el.scrollTop = el.scrollHeight;
}

// --- Reports ---
async function loadReports() {
  const data = await api('/api/outputs?limit=30');
  if (!data) return;

  const el = document.getElementById('reports-list');
  el.innerHTML = data.outputs.map(o => {
    const name = o.stem
      .replace(/__\d{8}-\d{6}$/, '')
      .replace(/_/g, ' ')
      .replace(/^\d+\s*/, '');
    return `
      <div class="report-item" onclick="loadReport('${o.name}')">
        <span class="report-name">${name || o.stem}</span>
        <span class="report-meta">${o.age_hours}h<br>${formatBytes(o.size)}</span>
      </div>`;
  }).join('');
}

async function loadReport(filename) {
  const data = await api(`/api/outputs/${encodeURIComponent(filename)}`);
  if (!data) return;

  document.getElementById('reports-list').style.display = 'none';
  const detail = document.getElementById('report-detail');
  detail.style.display = 'block';
  detail.innerHTML = `
    <button class="back-btn" onclick="closeReport()">← Back</button>
    <div class="report-content">${escapeHtml(data.content)}</div>`;
}

function closeReport() {
  document.getElementById('reports-list').style.display = 'block';
  document.getElementById('report-detail').style.display = 'none';
}

// --- Controls ---
async function loadServices() {
  const data = await api('/api/services');
  if (!data) return;

  const el = document.getElementById('control-services');
  el.innerHTML = data.services.map(s => {
    const shortName = s.name.replace('novacore-', '');
    const isActive = s.active === 'active';
    return `
      <div class="service-item">
        <div>
          <span class="service-name">${shortName}</span>
          <div style="font-size:11px;color:var(--text-dim)">${s.since !== '?' ? s.since : ''}</div>
        </div>
        <span class="service-badge ${isActive ? 'active' : 'inactive'}">${s.active}</span>
      </div>`;
  }).join('');
}

async function triggerHeartbeat() {
  const data = await api('/api/heartbeat/trigger', {
    method: 'POST',
    body: JSON.stringify({}),
  });
  if (data) toast('Heartbeat triggered!');
}

async function injectTask() {
  const title = prompt('Task title:');
  if (!title) return;
  const body = prompt('Task description:');
  if (body === null) return;

  const data = await api('/api/tasks', {
    method: 'POST',
    body: JSON.stringify({ title, body }),
  });
  if (data) toast(`Task created: ${data.stem}`);
}

// --- WebSocket ---
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => console.log('WS connected');
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.event === 'task_created') toast(`New task: ${msg.data.title}`);
      if (msg.event === 'chat_response') { /* handled by chat flow */ }
    } catch {}
  };
  ws.onclose = () => setTimeout(connectWS, 3000);
  ws.onerror = () => ws.close();

  // Ping every 30s
  setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) ws.send('ping');
  }, 30000);
}

// --- Helpers ---
function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function formatBytes(b) {
  if (b < 1024) return `${b}B`;
  if (b < 1048576) return `${(b / 1024).toFixed(1)}K`;
  return `${(b / 1048576).toFixed(1)}M`;
}

function timeAgo(dateStr) {
  try {
    const d = new Date(dateStr);
    const mins = Math.floor((Date.now() - d.getTime()) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
    return `${Math.floor(mins / 1440)}d ago`;
  } catch { return dateStr; }
}

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
  switchPage('dashboard');
  connectWS();

  // Auto-refresh dashboard every 60s
  setInterval(() => {
    if (currentPage === 'dashboard') loadDashboard();
  }, 60000);

  // Chat enter key
  document.getElementById('chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });

  // Register service worker
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(console.error);
  }
});
