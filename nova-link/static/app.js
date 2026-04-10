// Nova-Link — Modern AI app frontend
const API = window.location.origin;
let currentPage = 'chat';
let ws = null;

// --- Navigation ---
function switchPage(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(`page-${page}`).classList.add('active');
  document.querySelector(`[data-page="${page}"]`).classList.add('active');
  currentPage = page;

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
  setTimeout(() => el.classList.remove('show'), 2400);
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
  const isUnhealthy = hb.status && hb.status !== 'HEALTHY' && hb.status !== 'unknown';

  // Header status pill
  const pill = document.getElementById('status-pill');
  pill.className = `status-pill ${isHealthy ? 'healthy' : isUnhealthy ? 'unhealthy' : ''}`;
  document.getElementById('status-dot').className =
    `status-dot ${isHealthy ? 'healthy' : isUnhealthy ? 'unhealthy' : 'unknown'}`;
  document.getElementById('status-text').textContent =
    isHealthy ? 'HEALTHY' : (hb.status || 'UNKNOWN');

  // Hero pill on heartbeat card
  const heroPill = document.getElementById('hero-pill');
  if (heroPill) {
    heroPill.className = `hero-pill ${isHealthy ? 'healthy' : isUnhealthy ? 'unhealthy' : ''}`;
    document.getElementById('hero-pill-text').textContent =
      isHealthy ? 'HEALTHY' : (hb.status || 'UNKNOWN');
  }

  // Heartbeat card
  document.getElementById('hb-checks').textContent =
    `${hb.checks_passed}/${hb.checks_total}`;
  document.getElementById('hb-time').textContent =
    hb.last_check ? timeAgo(hb.last_check) : 'never';

  // Tasks
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
        <span class="check-icon">·</span>
        <span class="check-name">${escapeHtml(g.text || g.title || 'Goal')}</span>
        <span class="check-detail">${escapeHtml(g.priority || g.status || '')}</span>
      </div>`
    ).join('');
  } else {
    goalsEl.innerHTML = '<div class="check-item"><span style="color:var(--text-faint)">No active goals</span></div>';
  }

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
        <span class="check-icon">${c.ok ? '✓' : '✕'}</span>
        <span class="check-name">${escapeHtml(c.name)}</span>
        <span class="check-detail">${escapeHtml(c.detail)}</span>
      </div>`
    ).join('');
  }
  el.closest('.card').style.display = 'block';
}

// --- Chat state ---
const chatMessages = [];
let chatHistoryLoaded = false;
let activeThreadId = null;
const THREAD_KEY = 'nova-active-thread-id';
let chatMode = 'text';
let ttsEnabled = true;
let isListening = false;
let recognition = null;

// --- Markdown renderer (small subset, no deps) ---
function renderMarkdown(src) {
  if (!src) return '';
  const codeBlocks = [];
  const inlineCodes = [];

  // 1. Extract fenced code blocks
  let s = src.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push({ lang: lang || '', code });
    return `\u0000CODEBLOCK${idx}\u0000`;
  });

  // 2. Extract inline code
  s = s.replace(/`([^`\n]+)`/g, (_, code) => {
    const idx = inlineCodes.length;
    inlineCodes.push(code);
    return `\u0000INLINECODE${idx}\u0000`;
  });

  // 3. HTML-escape everything else
  s = escapeHtml(s);

  // 4. Inline transforms
  s = s
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
    .replace(/(?<![*\w])\*([^*\n]+?)\*(?!\w)/g, '<em>$1</em>')
    .replace(/(?<![_\w])_([^_\n]+?)_(?!\w)/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  // 5. Block transforms (line-by-line)
  const lines = s.split('\n');
  const out = [];
  let inList = null; // 'ul' | 'ol' | null
  let para = [];

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${para.join(' ')}</p>`);
      para = [];
    }
  };
  const closeList = () => {
    if (inList) { out.push(`</${inList}>`); inList = null; }
  };

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line.trim()) {
      flushPara();
      closeList();
      continue;
    }
    let m;
    if ((m = line.match(/^(#{1,3})\s+(.*)$/))) {
      flushPara(); closeList();
      out.push(`<h${m[1].length}>${m[2]}</h${m[1].length}>`);
      continue;
    }
    if ((m = line.match(/^[-*]\s+(.*)$/))) {
      flushPara();
      if (inList !== 'ul') { closeList(); out.push('<ul>'); inList = 'ul'; }
      out.push(`<li>${m[1]}</li>`);
      continue;
    }
    if ((m = line.match(/^\d+\.\s+(.*)$/))) {
      flushPara();
      if (inList !== 'ol') { closeList(); out.push('<ol>'); inList = 'ol'; }
      out.push(`<li>${m[1]}</li>`);
      continue;
    }
    closeList();
    para.push(line);
  }
  flushPara();
  closeList();

  let html = out.join('\n');

  // 6. Re-insert inline code (escaped)
  html = html.replace(/\u0000INLINECODE(\d+)\u0000/g, (_, idx) =>
    `<code>${escapeHtml(inlineCodes[+idx])}</code>`);

  // 7. Re-insert fenced code blocks (escaped)
  html = html.replace(/\u0000CODEBLOCK(\d+)\u0000/g, (_, idx) => {
    const { lang, code } = codeBlocks[+idx];
    const cls = lang ? ` class="lang-${escapeHtml(lang)}"` : '';
    return `<pre><code${cls}>${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`;
  });

  return html;
}

// --- Chat history loading ---
async function loadChatHistory() {
  if (chatHistoryLoaded) return;

  let thread = null;
  const saved = localStorage.getItem(THREAD_KEY);
  if (saved) {
    thread = await api(`/api/threads/${saved}`);
    if (!thread || !thread.id) thread = null;
  }
  if (!thread) {
    thread = await api('/api/threads/default');
  }

  if (thread && thread.id) {
    activeThreadId = thread.id;
    localStorage.setItem(THREAD_KEY, thread.id);
    updateThreadTitle(thread.title);
    await _loadThreadMessages(thread.id);
    chatHistoryLoaded = true;
    return;
  }

  // Fallback: legacy chat history endpoint
  const data = await api('/api/chat/history');
  if (!data || !data.messages) return;
  chatMessages.length = 0;
  for (const m of data.messages) {
    if (m.role === 'user' || m.role === 'assistant') {
      if (m.content && m.content.startsWith('[Prior conversation summary:')) continue;
      chatMessages.push({ role: m.role, content: m.content });
    }
  }
  chatHistoryLoaded = true;
  renderChat();
}

async function _loadThreadMessages(threadId) {
  const msgData = await api(`/api/threads/${threadId}/messages?limit=200&order=asc`);
  chatMessages.length = 0;
  if (msgData && msgData.messages) {
    for (const m of msgData.messages) {
      if (m.role === 'user' || m.role === 'assistant') {
        chatMessages.push({
          role: m.role,
          content: m.content,
          created_at: m.created_at,
        });
      }
    }
  }
  renderChat();
}

function updateThreadTitle(title) {
  const el = document.getElementById('chat-title');
  if (el) el.textContent = title || 'New conversation';
}

// --- Thread management ---
async function newConversation() {
  _stopJobPolling();
  _typingStartTime = null;

  const thread = await api('/api/threads', {
    method: 'POST',
    body: JSON.stringify({}),
  });
  if (!thread || !thread.id) {
    toast('Could not create conversation');
    return;
  }

  activeThreadId = thread.id;
  localStorage.setItem(THREAD_KEY, thread.id);
  updateThreadTitle(thread.title);
  chatMessages.length = 0;
  renderChat();
  chatHistoryLoaded = true;
  toast('New conversation started');
  document.getElementById('chat-input')?.focus();
}

function openSidebar() {
  const sb = document.getElementById('chat-sidebar');
  sb.classList.add('is-open');
  refreshThreadList();
}

function closeSidebar() {
  document.getElementById('chat-sidebar').classList.remove('is-open');
}

async function refreshThreadList() {
  const data = await api('/api/threads?status=active&limit=100');
  const el = document.getElementById('thread-list');
  if (!data || !data.threads || data.threads.length === 0) {
    el.innerHTML = '<div class="thread-list-empty">No conversations yet.</div>';
    return;
  }
  el.innerHTML = data.threads.map(t => {
    const title = escapeHtml(t.title || 'Untitled');
    const ts = t.updated_at ? formatTimestamp(t.updated_at) : '';
    const count = t.message_count || 0;
    const isActive = t.id === activeThreadId;
    const plural = count === 1 ? '' : 's';
    return `
      <div class="thread-item ${isActive ? 'active' : ''}" onclick="selectThread('${t.id}')">
        <div class="thread-item-title">${title}</div>
        <div class="thread-item-meta">${count} message${plural} · ${ts}</div>
      </div>`;
  }).join('');
}

async function selectThread(threadId) {
  if (threadId === activeThreadId) {
    closeSidebar();
    return;
  }

  _stopJobPolling();
  _typingStartTime = null;

  const thread = await api(`/api/threads/${threadId}`);
  if (!thread || !thread.id) {
    toast('Conversation not found');
    return;
  }

  activeThreadId = thread.id;
  localStorage.setItem(THREAD_KEY, thread.id);
  updateThreadTitle(thread.title);
  await _loadThreadMessages(thread.id);
  chatHistoryLoaded = true;
  closeSidebar();
}

// --- Async chat job handling ---
let _pendingJobId = null;
let _pendingJobTimer = null;
let _typingStartTime = null;
let _pollFailures = 0;
const _MAX_POLL_FAILURES = 5;
const _MAX_POLL_DURATION_S = 14400;

function _updateTypingIndicator() {
  const typingMsg = chatMessages.find(m => m.role === 'typing');
  if (!typingMsg || !_typingStartTime) return;
  const elapsed = Math.floor((Date.now() - _typingStartTime) / 1000);

  if (elapsed > _MAX_POLL_DURATION_S) {
    _deliverChatResponse(null, 'Response timed out — Nova may still be working. Try again in a moment.');
    return;
  }

  if (elapsed < 5) typingMsg.content = '';
  else if (elapsed < 30) typingMsg.content = `${elapsed}s`;
  else if (elapsed < 120) typingMsg.content = `${elapsed}s · working on a detailed response…`;
  else {
    const mins = Math.floor(elapsed / 60);
    typingMsg.content = `${mins}m ${elapsed % 60}s · deep processing…`;
  }
  renderChat();
}

function _deliverChatResponse(response, error, threadId) {
  _stopJobPolling();
  _typingStartTime = null;

  if (threadId && !activeThreadId) activeThreadId = threadId;

  const idx = chatMessages.findIndex(m => m.role === 'typing');
  if (idx !== -1) chatMessages.splice(idx, 1);

  const now = Date.now() / 1000;
  if (response) {
    chatMessages.push({ role: 'assistant', content: response, created_at: now });
    if (ttsEnabled) speak(response);
    _flashSendButton('success');
  } else {
    chatMessages.push({ role: 'assistant', content: error || 'No response received.', created_at: now });
  }
  renderChat();
  updateSendButtonState();
}

function _stopJobPolling() {
  if (_pendingJobTimer) { clearInterval(_pendingJobTimer); _pendingJobTimer = null; }
  _pendingJobId = null;
  _pollFailures = 0;
}

function _startJobPolling(jobId) {
  if (_pendingJobTimer) { clearInterval(_pendingJobTimer); _pendingJobTimer = null; }
  _pendingJobId = jobId;
  _pollFailures = 0;
  _pendingJobTimer = setInterval(() => {
    _pollChatJob(jobId);
    _updateTypingIndicator();
  }, 3000);
}

async function _pollChatJob(jobId) {
  try {
    const data = await api(`/api/chat/jobs/${jobId}`);
    if (!data || data.job_id !== jobId) {
      _pollFailures++;
      if (_pollFailures >= _MAX_POLL_FAILURES) {
        _deliverChatResponse(null, 'Lost connection to Nova — please try again.');
      }
      return;
    }
    if (_pendingJobId !== jobId) return;

    _pollFailures = 0;

    if (data.status === 'completed') {
      _deliverChatResponse(data.response || '(empty response)', null, data.thread_id);
    } else if (data.status === 'failed') {
      _deliverChatResponse(null, data.error || 'Processing failed.', data.thread_id);
    }
  } catch (e) {
    console.error('Poll error:', e);
    _pollFailures++;
    if (_pollFailures >= _MAX_POLL_FAILURES) {
      _deliverChatResponse(null, 'Lost connection to Nova — please try again.');
    }
  }
}

// --- Send chat ---
async function sendChat() {
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg) return;

  unlockTTS();
  input.value = '';
  autoResizeTextarea(input);

  // Optimistic title for fresh threads (mirrors backend auto-title)
  const realMsgs = chatMessages.filter(m => m.role !== 'typing');
  if (realMsgs.length === 0) {
    updateThreadTitle(msg.split('\n')[0].slice(0, 80));
  }

  chatMessages.push({ role: 'user', content: msg, created_at: Date.now() / 1000 });
  _typingStartTime = Date.now();
  chatMessages.push({ role: 'typing', content: '' });
  renderChat();
  updateSendButtonState();

  const data = await api('/api/chat', {
    method: 'POST',
    body: JSON.stringify({ message: msg, thread_id: activeThreadId }),
  });

  if (data && data.job_id) {
    _startJobPolling(data.job_id);
  } else {
    _deliverChatResponse(null, 'Failed to send message.');
  }
}

function sendPrompt(text) {
  const input = document.getElementById('chat-input');
  input.value = text;
  autoResizeTextarea(input);
  if (text.trim().endsWith('…')) {
    input.focus();
    updateSendButtonState();
    return;
  }
  sendChat();
}

// --- Render chat ---
function renderChat() {
  const empty = document.getElementById('chat-empty');
  const list = document.getElementById('chat-messages');
  const hasMsgs = chatMessages.some(m => m.role !== 'typing');
  const hasTyping = chatMessages.some(m => m.role === 'typing');

  if (!hasMsgs && !hasTyping) {
    empty.classList.add('is-visible');
    list.style.display = 'none';
    return;
  }

  empty.classList.remove('is-visible');
  list.style.display = 'flex';

  list.innerHTML = chatMessages.map((m, i) => {
    if (m.role === 'typing') {
      const detail = m.content ? `<span class="typing-text">${escapeHtml(m.content)}</span>` : '';
      return `<div class="chat-bubble typing"><div class="typing-dots"><span></span><span></span><span></span></div>${detail}</div>`;
    }
    if (m.role === 'user') {
      return `<div class="chat-bubble user">${escapeHtml(m.content)}</div>`;
    }
    // assistant
    const html = renderMarkdown(m.content);
    return `<div class="chat-bubble assistant">
      <div class="bubble-md">${html}</div>
      <button class="copy-btn" data-idx="${i}" onclick="copyMessage(this, ${i})" aria-label="Copy">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
        </svg>
      </button>
    </div>`;
  }).join('');
  list.scrollTop = list.scrollHeight;
}

// --- Copy message ---
async function copyMessage(btn, idx) {
  const msg = chatMessages[idx];
  if (!msg) return;
  try {
    await navigator.clipboard.writeText(msg.content);
    btn.classList.add('copied');
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
    toast('Copied');
    setTimeout(() => {
      btn.classList.remove('copied');
      btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
    }, 1500);
  } catch (e) {
    toast('Copy failed');
  }
}

// --- Auto-resize textarea ---
function autoResizeTextarea(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 200) + 'px';
}

// --- Send button state machine ---
function updateSendButtonState() {
  const btn = document.getElementById('send-btn');
  const input = document.getElementById('chat-input');
  if (!btn || !input) return;

  if (_pendingJobId) {
    btn.dataset.state = 'loading';
    btn.disabled = true;
    return;
  }
  if (!input.value.trim()) {
    btn.dataset.state = 'idle';
    btn.disabled = true;
    return;
  }
  btn.dataset.state = 'idle';
  btn.disabled = false;
}

function _flashSendButton(state) {
  const btn = document.getElementById('send-btn');
  if (!btn) return;
  btn.dataset.state = state;
  setTimeout(() => updateSendButtonState(), 1000);
}

// --- Voice mode ---
function setChatMode(mode) {
  unlockTTS();
  chatMode = mode;
  document.getElementById('composer-row').style.display = mode === 'text' ? 'flex' : 'none';
  document.getElementById('voice-input-bar').style.display = mode === 'voice' ? 'flex' : 'none';

  if (mode === 'text' && isListening) stopListening();
  if (mode === 'voice') initSpeechRecognition();
}

function toggleTTS() {
  ttsEnabled = !ttsEnabled;
  const btn = document.getElementById('tts-toggle');
  if (btn) btn.style.color = ttsEnabled ? 'var(--accent)' : 'var(--text-dim)';
  if (!ttsEnabled) window.speechSynthesis.cancel();
  toast(ttsEnabled ? 'Voice responses on' : 'Voice responses off');
}

let ttsUnlocked = false;
function unlockTTS() {
  if (ttsUnlocked || !('speechSynthesis' in window)) return;
  const utter = new SpeechSynthesisUtterance('');
  utter.volume = 0;
  window.speechSynthesis.speak(utter);
  ttsUnlocked = true;
}

function speak(text) {
  if (!('speechSynthesis' in window) || !ttsEnabled) return;
  window.speechSynthesis.cancel();

  const clean = text
    .replace(/```[\s\S]*?```/g, 'code block omitted')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/\*([^*]+)\*/g, '$1')
    .replace(/#{1,6}\s/g, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/[-*]\s/g, '')
    .trim();

  if (!clean) return;

  const chunks = [];
  const sentences = clean.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [clean];
  let current = '';
  for (const s of sentences) {
    if ((current + s).length > 200) {
      if (current) chunks.push(current.trim());
      current = s;
    } else {
      current += s;
    }
  }
  if (current.trim()) chunks.push(current.trim());

  const voice = getSelectedVoice();
  const rate = parseFloat(localStorage.getItem('nova-tts-speed') || '1');
  const pitch = parseFloat(localStorage.getItem('nova-tts-pitch') || '1');

  for (const chunk of chunks) {
    const utter = new SpeechSynthesisUtterance(chunk);
    utter.rate = rate;
    utter.pitch = pitch;
    if (voice) utter.voice = voice;
    window.speechSynthesis.speak(utter);
  }

  const keepAlive = setInterval(() => {
    if (!window.speechSynthesis.speaking) clearInterval(keepAlive);
    else window.speechSynthesis.resume();
  }, 5000);
}

// --- Voice settings ---
function getSelectedVoice() {
  const voices = window.speechSynthesis.getVoices();
  const saved = localStorage.getItem('nova-tts-voice');
  if (saved) {
    const match = voices.find(v => v.name === saved);
    if (match) return match;
  }
  return voices.find(v =>
    v.name.includes('Samantha') || v.name.includes('Karen') ||
    v.name.includes('Google') || v.name.includes('Natural')
  ) || voices.find(v => v.lang.startsWith('en')) || voices[0];
}

function populateVoiceSelect() {
  const select = document.getElementById('voice-select');
  if (!select) return;
  const voices = window.speechSynthesis.getVoices();
  if (!voices.length) return;

  const saved = localStorage.getItem('nova-tts-voice');
  select.innerHTML = '';

  const groups = {};
  for (const v of voices) {
    const lang = v.lang.split('-')[0].toUpperCase();
    if (!groups[lang]) groups[lang] = [];
    groups[lang].push(v);
  }

  const sortedLangs = Object.keys(groups).sort((a, b) => {
    if (a === 'EN') return -1;
    if (b === 'EN') return 1;
    return a.localeCompare(b);
  });

  for (const lang of sortedLangs) {
    const optgroup = document.createElement('optgroup');
    optgroup.label = lang;
    for (const v of groups[lang]) {
      const opt = document.createElement('option');
      opt.value = v.name;
      opt.textContent = v.name + (v.localService ? '' : ' (network)');
      if (v.name === saved) opt.selected = true;
      optgroup.appendChild(opt);
    }
    select.appendChild(optgroup);
  }

  if (!saved) {
    const def = getSelectedVoice();
    if (def) select.value = def.name;
  }
}

function toggleVoiceSettings() {
  const panel = document.getElementById('voice-settings');
  const isOpen = panel.style.display !== 'none';
  panel.style.display = isOpen ? 'none' : 'block';
  if (!isOpen) populateVoiceSelect();
}

function saveVoiceSetting() {
  const select = document.getElementById('voice-select');
  const speed = document.getElementById('voice-speed');
  const pitch = document.getElementById('voice-pitch');
  if (select.value) localStorage.setItem('nova-tts-voice', select.value);
  localStorage.setItem('nova-tts-speed', speed.value);
  localStorage.setItem('nova-tts-pitch', pitch.value);
}

function updateSpeedLabel() {
  const val = document.getElementById('voice-speed').value;
  document.getElementById('speed-label').textContent = parseFloat(val).toFixed(1) + 'x';
}

function updatePitchLabel() {
  const val = document.getElementById('voice-pitch').value;
  document.getElementById('pitch-label').textContent = parseFloat(val).toFixed(1) + 'x';
}

function testVoice() {
  unlockTTS();
  speak('Hey, this is Nova. How does this voice sound?');
}

function loadVoiceSettings() {
  const speed = localStorage.getItem('nova-tts-speed') || '1';
  const pitch = localStorage.getItem('nova-tts-pitch') || '1';
  const speedEl = document.getElementById('voice-speed');
  const pitchEl = document.getElementById('voice-pitch');
  if (speedEl) speedEl.value = speed;
  if (pitchEl) pitchEl.value = pitch;
  updateSpeedLabel();
  updatePitchLabel();
}

// --- Speech recognition ---
function initSpeechRecognition() {
  if (recognition) return;

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    toast('Speech recognition not supported');
    setChatMode('text');
    return;
  }

  recognition = new SpeechRecognition();
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.lang = 'en-US';

  recognition.onresult = (e) => {
    const transcript = Array.from(e.results).map(r => r[0].transcript).join('');
    document.getElementById('voice-transcript').textContent = transcript;
    if (e.results[e.results.length - 1].isFinal && transcript.trim()) {
      sendVoiceMessage(transcript.trim());
    }
  };

  recognition.onstart = () => {
    isListening = true;
    document.getElementById('mic-btn').classList.add('listening');
    document.getElementById('voice-status').textContent = 'Listening…';
    document.getElementById('voice-transcript').textContent = '';
  };

  recognition.onend = () => {
    isListening = false;
    document.getElementById('mic-btn').classList.remove('listening');
    document.getElementById('voice-status').textContent = 'Tap to speak';
  };

  recognition.onerror = (e) => {
    isListening = false;
    document.getElementById('mic-btn').classList.remove('listening');
    if (e.error === 'not-allowed') {
      document.getElementById('voice-status').textContent = 'Mic access denied — check settings';
    } else {
      document.getElementById('voice-status').textContent = 'Tap to speak';
    }
  };
}

function toggleVoice() {
  if (isListening) stopListening();
  else startListening();
}

function startListening() {
  if (!recognition) initSpeechRecognition();
  if (!recognition) return;
  unlockTTS();
  window.speechSynthesis.cancel();
  try { recognition.start(); } catch { /* already started */ }
}

function stopListening() {
  if (recognition && isListening) {
    try { recognition.stop(); } catch { /* already stopped */ }
  }
}

async function sendVoiceMessage(text) {
  chatMessages.push({ role: 'user', content: text, created_at: Date.now() / 1000 });
  _typingStartTime = Date.now();
  chatMessages.push({ role: 'typing', content: '' });
  renderChat();

  document.getElementById('voice-status').textContent = 'Nova is thinking…';
  document.getElementById('voice-transcript').textContent = '';

  const data = await api('/api/chat', {
    method: 'POST',
    body: JSON.stringify({ message: text, thread_id: activeThreadId }),
  });

  if (data && data.job_id) {
    _startJobPolling(data.job_id);
  } else {
    _deliverChatResponse(null, 'Failed to send message.');
  }
}

// --- Reports ---
async function loadReports() {
  const data = await api('/api/outputs?limit=30');
  if (!data) return;

  const el = document.getElementById('reports-list');
  el.innerHTML = data.outputs.map(o => {
    const name = o.stem.replace(/__\d{8}-\d{6}$/, '').replace(/_/g, ' ').replace(/^\d+\s*/, '');
    return `
      <div class="report-item" onclick="loadReport('${o.name}')">
        <span class="report-name">${escapeHtml(name || o.stem)}</span>
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
    <button class="back-btn" onclick="closeReport()">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
        <line x1="19" y1="12" x2="5" y2="12"/>
        <polyline points="12 19 5 12 12 5"/>
      </svg>
      Back
    </button>
    <div class="report-content bubble-md">${renderMarkdown(data.content)}</div>`;
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
          <span class="service-name">${escapeHtml(shortName)}</span>
          <div style="font-size:11px;color:var(--text-faint)">${s.since !== '?' ? escapeHtml(s.since) : ''}</div>
        </div>
        <span class="service-badge ${isActive ? 'active' : 'inactive'}">${escapeHtml(s.active)}</span>
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

  ws.onopen = () => {
    if (_pendingJobId) _pollChatJob(_pendingJobId);
  };
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.event === 'task_created') toast(`New task: ${msg.data.title}`);
      if (msg.event === 'chat_response' && msg.data) {
        const d = msg.data;
        if (_pendingJobId && d.job_id === _pendingJobId) {
          if (d.response) _deliverChatResponse(d.response, null, d.thread_id);
          else if (d.error) _deliverChatResponse(null, d.error, d.thread_id);
        }
      }
    } catch {}
  };
  ws.onclose = () => setTimeout(connectWS, 3000);
  ws.onerror = () => ws.close();

  setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) ws.send('ping');
  }, 30000);
}

// --- Helpers ---
function escapeHtml(str) {
  if (str == null) return '';
  const div = document.createElement('div');
  div.textContent = String(str);
  return div.innerHTML;
}

function formatTimestamp(unix) {
  const d = new Date(unix * 1000);
  const now = new Date();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return time;
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return `Yesterday ${time}`;
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
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
  switchPage('chat');
  connectWS();

  // Auto-refresh dashboard every 60s when visible
  setInterval(() => {
    if (currentPage === 'dashboard') loadDashboard();
  }, 60000);

  // Composer wiring
  const input = document.getElementById('chat-input');
  if (input) {
    input.addEventListener('input', () => {
      autoResizeTextarea(input);
      updateSendButtonState();
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChat();
      }
    });
    autoResizeTextarea(input);
  }
  updateSendButtonState();

  // Sidebar dismiss with Escape
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const sb = document.getElementById('chat-sidebar');
      if (sb && sb.classList.contains('is-open')) closeSidebar();
    }
  });

  // TTS init
  if ('speechSynthesis' in window) {
    window.speechSynthesis.getVoices();
    window.speechSynthesis.onvoiceschanged = () => {
      window.speechSynthesis.getVoices();
      populateVoiceSelect();
    };
  }
  loadVoiceSettings();
  const ttsBtn = document.getElementById('tts-toggle');
  if (ttsBtn) ttsBtn.style.color = ttsEnabled ? 'var(--accent)' : 'var(--text-dim)';

  // Service worker
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(console.error);
  }
});
