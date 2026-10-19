/* ==========================================================================
   待办小助理 · 前端逻辑
   原生 JS（无构建链）：SSE 流式对话 + 任务 CRUD + 今日计划 + 设置页
   ========================================================================== */
(function () {
  'use strict';

  /* ----------------------------- 基础工具 ----------------------------- */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const ESCAPE_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ESCAPE_MAP[c]);

  /**
   * 轻量 Markdown 渲染器。
   * 只支持模型实际会输出的子集：标题、粗体、斜体、行内代码、
   * 有序/无序列表、引用、表格、链接、段落。先转义再解析，避免 XSS。
   */
  function mdToHtml(src) {
    const lines = String(src || '').split(/\r?\n/);
    const out = [];
    let list = null;      // 'ul' | 'ol' | null
    let inTable = false;
    let tableRows = 0;

    const closeList = () => { if (list) { out.push('</' + list + '>'); list = null; } };
    const closeTable = () => { if (inTable) { out.push('</tbody></table>'); inTable = false; tableRows = 0; } };

    const inline = (text) => esc(text)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\*([^*\n]+)\*/g, '<em>$1</em>')
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

    for (let i = 0; i < lines.length; i++) {
      const raw = lines[i];
      const line = raw.trim();

      // 表格
      if (/^\|.*\|$/.test(line)) {
        const cells = line.slice(1, -1).split('|').map((c) => c.trim());
        if (!inTable) {
          closeList();
          out.push('<table><thead><tr>' + cells.map((c) => '<th>' + inline(c) + '</th>').join('') + '</tr></thead><tbody>');
          inTable = true;
          tableRows = 0;
          continue;
        }
        if (/^[\s:|-]+$/.test(line)) continue; // 分隔行
        out.push('<tr>' + cells.map((c) => '<td>' + inline(c) + '</td>').join('') + '</tr>');
        tableRows += 1;
        continue;
      }
      closeTable();

      if (!line) { closeList(); continue; }

      // 标题
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        closeList();
        out.push('<strong>' + inline(heading[2]) + '</strong>');
        continue;
      }

      // 引用
      if (/^>\s?/.test(line)) { closeList(); out.push('<blockquote>' + inline(line.replace(/^>\s?/, '')) + '</blockquote>'); continue; }

      // 无序列表
      const ul = line.match(/^[-*·]\s+(.*)$/);
      if (ul) {
        if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; }
        out.push('<li>' + inline(ul[1]) + '</li>');
        continue;
      }

      // 有序列表
      const ol = line.match(/^\d+[.、)]\s+(.*)$/);
      if (ol) {
        if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; }
        out.push('<li>' + inline(ol[1]) + '</li>');
        continue;
      }

      closeList();
      out.push('<p>' + inline(line) + '</p>');
    }
    closeList();
    closeTable();
    return out.join('') || '<p class="muted">（无内容）</p>';
  }

  /* ----------------------------- 提示条 ----------------------------- */
  function toast(message, kind) {
    const wrap = $('#toast-wrap');
    const el = document.createElement('div');
    el.className = 'toast' + (kind ? ' is-' + kind : '');
    el.innerHTML = esc(message);
    wrap.appendChild(el);
    setTimeout(() => {
      el.classList.add('is-leaving');
      setTimeout(() => el.remove(), 260);
    }, kind === 'error' ? 5200 : 3200);
  }

  /* ----------------------------- 接口封装 ----------------------------- */
  async function request(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = 'HTTP ' + response.status;
      try {
        const payload = await response.json();
        if (payload && payload.detail) detail = typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail);
      } catch (e) { /* 忽略解析失败 */ }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  const api = {
    get: (url) => request(url),
    post: (url, body) => request(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }),
    patch: (url, body) => request(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }),
    del: (url) => request(url, { method: 'DELETE' }),
  };

  /* ----------------------------- 全局状态 ----------------------------- */
  const state = {
    view: 'chat',
    sessionId: localStorage.getItem('todo_session') || null,
    filters: { status: 'open', priority: '', keyword: '', tag: '', only_overdue: false },
    layout: 'list',
    tasks: [],
    todayDate: null,
    meta: null,
    sending: false,
    theme: localStorage.getItem('todo_theme') || 'light',
  };

  const VIEW_META = {
    chat: { title: '对话工作台', sub: '用一句话说出你想做什么，我来帮你落地' },
    today: { title: '今日计划', sub: '时间轴日程、天气与出行建议、逾期提醒' },
    tasks: { title: '任务总览', sub: '筛选、编辑、完成任务，支持列表与看板两种视图' },
    settings: { title: '设置与关于', sub: '运行环境、MCP 工具清单、加分项实现情况' },
  };

  /* ----------------------------- 视图切换 ----------------------------- */
  function switchView(view) {
    state.view = view;
    $$('.nav-item').forEach((btn) => btn.classList.toggle('is-active', btn.dataset.view === view));
    $$('.view').forEach((section) => section.classList.toggle('is-active', section.id === 'view-' + view));
    $('#view-title').textContent = VIEW_META[view].title;
    $('#view-sub').textContent = VIEW_META[view].sub;
    $('#app').classList.remove('nav-open');

    if (view === 'today') loadToday();
    if (view === 'tasks') loadTasks();
    if (view === 'settings') loadMeta();
  }

  /* ----------------------------- 主题 ----------------------------- */
  function applyTheme(theme) {
    state.theme = theme;
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('todo_theme', theme);
    $('#theme-icon').innerHTML = theme === 'dark'
      ? '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>'
      : '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>';
  }

  /* ----------------------------- 对话 ----------------------------- */
  const messagesEl = () => $('#messages');

  function scrollToBottom() {
    const el = messagesEl();
    el.scrollTop = el.scrollHeight;
  }

  function messageTemplate(role, innerHtml) {
    const wrap = document.createElement('div');
    wrap.className = 'msg msg-' + role;
    wrap.innerHTML =
      '<div class="msg-avatar">' + (role === 'user' ? '我' : '✓') + '</div>' +
      '<div style="min-width:0">' + innerHtml + '</div>';
    return wrap;
  }

  function addUserMessage(text) {
    const node = messageTemplate('user', '<div class="msg-bubble">' + esc(text).replace(/\n/g, '<br>') + '</div>');
    messagesEl().appendChild(node);
    scrollToBottom();
  }

  function addAssistantMessage() {
    const node = messageTemplate('assistant',
      '<div class="msg-bubble"><span class="typing"><i></i><i></i><i></i></span></div>' +
      '<div class="trace"></div>');
    messagesEl().appendChild(node);
    scrollToBottom();
    return {
      root: node,
      bubble: $('.msg-bubble', node),
      trace: $('.trace', node),
    };
  }

  function addToolCard(trace, event) {
    const card = document.createElement('div');
    card.className = 'tool-card';
    const argText = JSON.stringify(event.arguments || {});
    card.innerHTML =
      '<div class="tool-head">' +
        '<svg class="tool-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a4 4 0 0 0 5 5l-9.4 9.4a2.1 2.1 0 0 1-3-3l9.4-9.4Z"/><path d="M14.7 6.3 18 3l3 3-3.3 3.3"/></svg>' +
        '<span class="tool-name">' + esc(event.name) + '</span>' +
        '<span class="tool-args">' + esc(argText.length > 90 ? argText.slice(0, 90) + '…' : argText) + '</span>' +
        '<span class="tool-status running"><span class="spinner"></span>执行中</span>' +
      '</div>' +
      '<div class="tool-result">正在调用 MCP 工具…</div>';
    card.addEventListener('click', () => card.classList.toggle('is-open'));
    trace.appendChild(card);
    scrollToBottom();
    return card;
  }

  function finishToolCard(card, event) {
    const status = $('.tool-status', card);
    const ok = event.ok !== false;
    status.className = 'tool-status ' + (ok ? 'ok' : 'err');
    status.textContent = (ok ? '✓ 完成 · ' : '✗ 失败 · ') + (event.duration_ms || 0) + 'ms';
    if (!ok) card.classList.add('is-error');
    $('.tool-result', card).textContent = event.summary || event.output || '（无输出）';
    if (!ok) card.classList.add('is-open');
  }

  function currentSessionId() {
    if (!state.sessionId) {
      state.sessionId = 'web-' + Math.random().toString(36).slice(2, 10);
      localStorage.setItem('todo_session', state.sessionId);
    }
    return state.sessionId;
  }

  function updateSessionBadge() {
    $('#session-badge').textContent = '会话 ' + (state.sessionId ? state.sessionId.slice(0, 12) : '—');
  }

  async function sendMessage(text) {
    if (state.sending) { toast('上一条还在处理中，请稍等', 'error'); return; }
    const message = (text || '').trim();
    if (!message) return;

    state.sending = true;
    $('#send').disabled = true;
    $('#input').value = '';
    autoGrow($('#input'));
    addUserMessage(message);

    const view = addAssistantMessage();
    let buffer = '';
    let toolMapIndex = 0;
    const toolCards = new Map();

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: message, session_id: currentSessionId() }),
      });
      if (!response.ok) {
        let detail = 'HTTP ' + response.status;
        try { const p = await response.json(); detail = p.detail || detail; } catch (e) { /* noop */ }
        throw new Error(detail);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let raw = '';

      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        raw += decoder.decode(chunk.value, { stream: true });

        const frames = raw.split('\n\n');
        raw = frames.pop() || '';
        for (const frame of frames) {
          const dataLine = frame.split('\n').find((line) => line.startsWith('data:'));
          if (!dataLine) continue;
          let event;
          try { event = JSON.parse(dataLine.slice(5).trim()); } catch (e) { continue; }
          handleEvent(event, view, () => buffer, (value) => { buffer = value; }, toolCards);
        }
      }
    } catch (error) {
      view.bubble.innerHTML = '<strong style="color:var(--danger)">请求失败</strong><p>' + esc(error.message) + '</p>';
      toast(error.message, 'error');
    } finally {
      state.sending = false;
      $('#send').disabled = false;
      $('#input').focus();
      refreshSidePanels();
    }

    function handleEvent(event, view, getBuffer, setBuffer, cards) {
      switch (event.type) {
        case 'start':
          if (event.session_id) {
            state.sessionId = event.session_id;
            localStorage.setItem('todo_session', event.session_id);
            updateSessionBadge();
          }
          break;

        case 'text_delta':
          setBuffer(getBuffer() + (event.text || ''));
          view.bubble.innerHTML = mdToHtml(getBuffer()) + '<span class="caret"></span>';
          scrollToBottom();
          break;

        case 'tool_call': {
          const card = addToolCard(view.trace, event);
          cards.set(event.id || ('t' + toolMapIndex++), card);
          break;
        }

        case 'tool_result': {
          const card = cards.get(event.id) || addToolCard(view.trace, event);
          finishToolCard(card, event);
          break;
        }

        case 'final':
          setBuffer(event.text || getBuffer());
          view.bubble.innerHTML = mdToHtml(getBuffer());
          scrollToBottom();
          // 广播给语音模块，用于注入喇叭按钮与触发自动朗读
          document.dispatchEvent(new CustomEvent('assistant-final', {
            detail: { text: getBuffer(), node: view.root, sessionId: state.sessionId },
          }));
          break;

        case 'error':
          toast(event.message || '处理失败', 'error');
          if (!getBuffer()) {
            view.bubble.innerHTML = '<strong style="color:var(--danger)">出错了</strong><p>' + esc(event.message || '') + '</p>';
          }
          break;

        case 'done':
          if (event.usage && event.usage.total_tokens) {
            $('#sb-status').textContent = '本轮 ' + (event.turns || 1) + ' 次模型调用 · ' + (event.usage.total_tokens) + ' tokens';
          }
          break;

        case 'end':
          if (view.bubble.querySelector('.typing')) {
            view.bubble.innerHTML = mdToHtml(getBuffer() || '（本轮没有产生回复）');
          }
          break;

        default:
          break;
      }
    }
  }

  async function restoreHistory() {
    if (!state.sessionId) return;
    try {
      const data = await api.get('/api/chat/history?session_id=' + encodeURIComponent(state.sessionId));
      const messages = (data.messages || []).filter((m) => (m.content || '').trim() && m.role !== 'tool');
      if (!messages.length) return;
      messagesEl().innerHTML = '';
      messages.forEach((m) => {
        if (m.role === 'user') {
          addUserMessage(m.content);
        } else {
          messagesEl().appendChild(messageTemplate('assistant', '<div class="msg-bubble">' + mdToHtml(m.content) + '</div><div class="trace"></div>'));
        }
      });
      scrollToBottom();
    } catch (error) {
      // 会话不存在等情况直接忽略，保持欢迎语
    }
  }

  /* ----------------------------- 今日计划 ----------------------------- */
  const WEATHER_ICONS = [
    [/雪/, '❄️'], [/雷/, '⛈️'], [/雾|霾/, '🌫️'],
    [/暴雨|大雨|中雨|阵雨|冻雨/, '🌧️'], [/毛毛雨|小雨/, '🌦️'],
    [/阴/, '☁️'], [/少云|晴间多云/, '⛅'], [/晴/, '☀️'],
  ];

  function weatherIcon(description) {
    for (const [pattern, icon] of WEATHER_ICONS) {
      if (pattern.test(description || '')) return icon;
    }
    return '🌤️';
  }

  function shiftDate(offsetDays) {
    const base = state.todayDate ? new Date(state.todayDate + 'T00:00:00') : new Date();
    base.setDate(base.getDate() + offsetDays);
    return base.toISOString().slice(0, 10);
  }

  async function loadToday(date) {
    const target = date || state.todayDate || null;
    const url = '/api/tasks/today' + (target ? '?date=' + encodeURIComponent(target) : '');
    try {
      const data = await api.get(url);
      state.todayDate = data.date;
      renderToday(data);
      loadWeather(data.date);
    } catch (error) {
      toast('加载今日计划失败：' + error.message, 'error');
    }
  }

  function renderToday(data) {
    $('#day-title').textContent = data.date_human + ' · ' + data.date;
    const done = (data.due_today || []).filter((t) => t.status === 'done').length;
    const total = (data.due_today || []).length;
    $('#day-sub').textContent = '今日任务 ' + total + ' 条，已完成 ' + done + ' 条，逾期 ' + (data.overdue || []).length + ' 条';

    // 时间轴
    const plan = data.plan || {};
    const timeline = plan.timeline || [];
    const timelineEl = $('#timeline');
    if (!timeline.length) {
      timelineEl.innerHTML = '<div class="empty"><strong>今天没有排入的任务</strong>点右上角「重新规划」按优先级生成时间轴</div>';
    } else {
      timelineEl.innerHTML = timeline.map((item) => (
        '<div class="tl-item prio-' + esc(item.priority) + '">' +
          '<div class="tl-time">' + esc(item.start_time) + '<br>– ' + esc(item.end_time) + '</div>' +
          '<div class="tl-body">' +
            '<div class="tl-title">#' + item.id + ' ' + esc(item.title) + '</div>' +
            '<div class="tl-sub">' + esc(item.priority_label) + '优先级 · 预计 ' + item.estimated_minutes + ' 分钟 · ' +
              (item.due_human ? '截止 ' + esc(item.due_human) : '无截止时间') +
              (item.before_due ? ' · 可在截止前完成' : '') +
            '</div>' +
          '</div>' +
        '</div>'
      )).join('');
    }
    $('#plan-badge').textContent = timeline.length + ' 条 · ' + (plan.total_minutes || 0) + ' 分钟';

    // 容量
    const capacity = plan.capacity_minutes || 0;
    const used = plan.total_minutes || 0;
    const percent = plan.utilization_percent || 0;
    $('#capacity-label').textContent = '已排 ' + used + ' / 可用 ' + capacity + ' 分钟';
    $('#capacity-value').textContent = percent + '%';
    $('#capacity-fill').style.width = Math.min(100, percent) + '%';
    const notes = [];
    if (plan.exhausted) notes.push('今天剩余时间已不足，建议改为安排明天。');
    if (plan.auto_adjusted && plan.day_start) notes.push('已按当前时间从 ' + plan.day_start.slice(11, 16) + ' 起排。');
    if (plan.lunch_break) notes.push('午休 ' + plan.lunch_break + ' 已自动跳过；每个任务之间有缓冲时间。');
    $('#capacity-note').textContent = notes.length ? notes.join(' ') : '未获取到排期信息。';

    // 无法排入
    const unscheduled = plan.unscheduled || [];
    $('#unscheduled-card').style.display = unscheduled.length ? '' : 'none';
    $('#unscheduled-list').innerHTML = unscheduled.map((item) => (
      '<div class="overdue-item" style="background:var(--surface-sunken);border-color:var(--border)">' +
        '<span class="dot" style="background:var(--warning)"></span>' +
        '<span class="t">#' + item.id + ' ' + esc(item.title) + '</span>' +
        '<span class="w" style="color:var(--warning)">' + esc(item.reason || '未排入') + '</span>' +
      '</div>'
    )).join('');

    // 逾期
    const overdue = data.overdue || [];
    $('#overdue-count').textContent = overdue.length;
    $('#overdue-list').innerHTML = overdue.length
      ? overdue.map((task) => (
          '<div class="overdue-item">' +
            '<span class="dot"></span>' +
            '<span class="t">#' + task.id + ' ' + esc(task.title) + '</span>' +
            '<span class="w">' + esc(task.due_human || '') + ' 已过期</span>' +
          '</div>'
        )).join('')
      : '<div class="empty" style="padding:18px">没有逾期任务 🎉</div>';
  }

  async function loadWeather(date) {
    $('#weather-desc').textContent = '正在获取天气…';
    try {
      const data = await api.get('/api/weather?date=' + encodeURIComponent(date));
      if (data.ok === false) throw new Error(data.error || '天气查询失败');
      $('#weather-icon').textContent = weatherIcon(data.description);
      $('#weather-temp').innerHTML = Math.round(data.temperature_max) + '°<small> / ' + Math.round(data.temperature_min) + '°C</small>';
      $('#weather-desc').textContent = data.city + ' · ' + data.description;
      $('#weather-meta').innerHTML =
        '<span>降水概率 ' + esc(data.precipitation_probability) + '%</span>' +
        '<span>最大风速 ' + esc(data.wind_speed_max) + ' km/h</span>' +
        '<span class="tag">' + esc(data.source) + '</span>';
      $('#weather-advice').textContent = data.advice || '—';
    } catch (error) {
      $('#weather-icon').textContent = '🌐';
      $('#weather-desc').textContent = '天气获取失败';
      $('#weather-advice').textContent = error.message;
    }
  }

  /* ------------------------ 直接动作：查天气 ------------------------ */

  const GEO_TIMEOUT = 4000;

  /** 取浏览器定位；不支持 / 被拒绝 / 超时都返回 null，绝不阻塞主流程。 */
  function currentPosition() {
    return new Promise((resolve) => {
      if (!navigator.geolocation) return resolve(null);
      let settled = false;
      const done = (value) => {
        if (settled) return;
        settled = true;
        resolve(value);
      };
      setTimeout(() => done(null), GEO_TIMEOUT);
      navigator.geolocation.getCurrentPosition(
        (pos) => done({ lat: pos.coords.latitude, lon: pos.coords.longitude }),
        () => done(null),
        { timeout: GEO_TIMEOUT, maximumAge: 300000 }
      );
    });
  }

  /**
   * 决定这次查哪里的天气，优先级：
   *   ① 偏好里的默认城市（用户明确设置过，最该尊重，也不再打扰他）
   *   ② 浏览器定位（"查我这儿的天气"）
   *   ③ 服务端默认城市（兜底，并在卡片上提示怎么固定城市）
   */
  async function resolveWeatherTarget() {
    let city = '';
    try {
      const prefs = await api.get('/api/preferences');
      city = (prefs.preferences || {}).default_city || '';
    } catch (error) { /* 偏好读不到不影响查天气 */ }
    if (city) return { mode: 'preference', city: city };

    const here = await currentPosition();
    if (here) return { mode: 'geo', lat: here.lat, lon: here.lon };
    return { mode: 'default' };
  }

  function weatherSourceNote(data, target) {
    const engine = '直接调用天气接口（' + (data.source || 'Open-Meteo') + '），未经过大模型';
    if (target.mode === 'preference') return engine + ' · 用的是你偏好里的默认城市';
    if (target.mode === 'geo') return engine + ' · 按浏览器定位到当前位置';
    return engine + ' · 未设置偏好城市，暂用默认城市；说一句「以后问天气默认用杭州」就能固定';
  }

  function weatherBodyHtml(data, target) {
    return (
      '<strong>' + weatherIcon(data.description) + ' ' + esc(data.city) +
        ' · ' + esc(data.date_human || data.date) + '</strong>' +
      '<p>' + esc(data.description) + '，' +
        Math.round(data.temperature_min) + '~' + Math.round(data.temperature_max) + '℃，' +
        '降水概率 ' + esc(data.precipitation_probability) + '%，' +
        '最大风速 ' + esc(data.wind_speed_max) + ' km/h</p>' +
      '<p>' + esc(data.advice) + '</p>' +
      '<p class="muted" style="font-size:12px">' + esc(weatherSourceNote(data, target)) + '</p>'
    );
  }

  /** 点「查天气」：真的去查一次，把结果作为一条助理消息插进对话。 */
  async function runWeatherChip(chip) {
    const label = chip.textContent;
    chip.disabled = true;
    chip.textContent = '查询中…';
    addUserMessage(label);

    const slot = addAssistantMessage();   // 先占位（自带打字动画），拿到结果再替换
    try {
      const target = await resolveWeatherTarget();
      const params = new URLSearchParams();
      if (target.city) params.set('city', target.city);
      if (target.lat != null) {
        params.set('lat', String(target.lat));
        params.set('lon', String(target.lon));
      }
      const query = params.toString();
      const data = await api.get('/api/weather' + (query ? '?' + query : ''));
      if (data.ok === false) throw new Error(data.error || '天气查询失败');

      slot.bubble.innerHTML = weatherBodyHtml(data, target);
      scrollToBottom();
      // 与正常回复保持一致：注入喇叭按钮，并按设置决定是否朗读
      document.dispatchEvent(new CustomEvent('assistant-final', {
        detail: { text: slot.bubble.textContent.trim(), node: slot.root, sessionId: state.sessionId },
      }));
    } catch (error) {
      slot.bubble.innerHTML =
        '<strong style="color:var(--danger)">天气没查到</strong><p>' + esc(error.message || error) + '</p>';
      toast('天气查询失败：' + (error.message || error), 'error');
      scrollToBottom();
    } finally {
      chip.disabled = false;
      chip.textContent = label;
    }
  }

  /* ----------------------------- 任务总览 ----------------------------- */
  async function loadTasks() {
    const f = state.filters;
    const query = new URLSearchParams({
      status: f.status,
      only_overdue: String(!!f.only_overdue),
      limit: '300',
    });
    if (f.priority) query.set('priority', f.priority);
    if (f.keyword) query.set('keyword', f.keyword);
    if (f.tag) query.set('tag', f.tag);

    try {
      const data = await api.get('/api/tasks?' + query.toString());
      state.tasks = data.tasks || [];
      renderSummary(data.stats || {});
      renderTaskLists();
    } catch (error) {
      toast('加载任务失败：' + error.message, 'error');
    }
  }

  function renderSummary(stats) {
    const items = [
      { label: '全部', value: stats.total || 0, cls: '' },
      { label: '未完成', value: stats.open || 0, cls: 'info' },
      { label: '进行中', value: stats.doing || 0, cls: '' },
      { label: '已完成', value: stats.done || 0, cls: 'success' },
      { label: '逾期', value: stats.overdue || 0, cls: 'danger' },
      { label: '今日完成', value: stats.done_today || 0, cls: 'success' },
    ];
    $('#summary-strip').innerHTML = items.map((item) => (
      '<div class="stat-pill ' + item.cls + '"><b>' + item.value + '</b><span>' + item.label + '</span></div>'
    )).join('');
  }

  function priorityBadge(task) {
    return '<span class="badge badge-' + esc(task.priority) + '">' + esc(task.priority_label) + '</span>';
  }

  function renderTaskLists() {
    const list = $('#task-list');
    const board = $('#task-board');
    list.style.display = state.layout === 'list' ? '' : 'none';
    board.style.display = state.layout === 'board' ? '' : 'none';
    if (state.layout === 'board') renderBoard();
    else renderList();
  }

  function taskRowHtml(task) {
    const tags = (task.tags || []).map((t) => '<span class="tag">' + esc(t) + '</span>').join(' ');
    return (
      '<div class="task-check" data-action="complete" data-id="' + task.id + '" title="标记完成">✓</div>' +
      '<div class="task-main">' +
        '<div class="task-title">#' + task.id + ' ' + esc(task.title) + '</div>' +
        '<div class="task-meta">' +
          priorityBadge(task) +
          (task.status === 'doing' ? '<span class="badge">进行中</span>' : '') +
          (task.status === 'done' ? '<span class="badge badge-done">已完成</span>' : '') +
          (task.due_date ? '<span class="' + (task.overdue ? 'late' : '') + '">截止 ' + esc(task.due_human) + (task.overdue ? ' · 已逾期' : '') + '</span>' : '<span>无截止时间</span>') +
          '<span>预计 ' + task.estimated_minutes + ' 分钟</span>' +
          (tags ? '<span>' + tags + '</span>' : '') +
          (task.notes ? '<span>· ' + esc(task.notes.slice(0, 24)) + '</span>' : '') +
        '</div>' +
      '</div>' +
      '<div class="task-actions">' +
        '<button class="mini-btn" data-action="edit" data-id="' + task.id + '" title="编辑">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5Z"/></svg>' +
        '</button>' +
        '<button class="mini-btn danger" data-action="delete" data-id="' + task.id + '" title="删除">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>' +
        '</button>' +
      '</div>'
    );
  }

  function renderList() {
    const list = $('#task-list');
    if (!state.tasks.length) {
      list.innerHTML = '<div class="card empty"><strong>没有符合条件的任务</strong>换个筛选条件，或者去对话工作台说一句「帮我记一下…」</div>';
      return;
    }
    list.innerHTML = state.tasks.map((task) => (
      '<div class="task-row' + (task.status === 'done' ? ' is-done' : '') + (task.overdue ? ' is-overdue' : '') +
        '" data-row="' + task.id + '">' +
        taskRowHtml(task) +
      '</div>'
    )).join('');
  }

  function renderBoard() {
    const columns = [
      { key: 'todo', label: '待办' },
      { key: 'doing', label: '进行中' },
      { key: 'done', label: '已完成' },
    ];
    const board = $('#task-board');
    board.innerHTML = columns.map((column) => {
      const items = state.tasks.filter((task) => task.status === column.key);
      const cards = items.map((task) => (
        '<div class="board-card" data-action="edit" data-id="' + task.id + '">' +
          '<div class="t">#' + task.id + ' ' + esc(task.title) + '</div>' +
          '<div class="m">' + priorityBadge(task) +
            (task.due_human ? '<span class="' + (task.overdue ? 'late' : '') + '">' + esc(task.due_human) + '</span>' : '<span>无期限</span>') +
            '<span>' + task.estimated_minutes + '′</span>' +
          '</div>' +
        '</div>'
      )).join('') || '<div class="empty" style="padding:14px">空</div>';
      return (
        '<div class="board-col">' +
          '<div class="board-head">' + column.label + '<span class="count">' + items.length + '</span></div>' +
          '<div class="board-body">' + cards + '</div>' +
        '</div>'
      );
    }).join('');
  }

  /* ----------------------------- 模态框 ----------------------------- */
  function openModal(options) {
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    const gridClass = options.twoColumn ? 'form-grid' : '';
    backdrop.innerHTML =
      '<div class="modal">' +
        '<h3>' + esc(options.title) + '</h3>' +
        '<p class="hint">' + esc(options.hint || '') + '</p>' +
        '<form class="' + gridClass + '" id="modal-form"></form>' +
        '<div class="modal-actions">' +
          '<button class="btn" id="modal-cancel">取消</button>' +
          '<button class="btn btn-primary" id="modal-submit">' + esc(options.submitText || '保存') + '</button>' +
        '</div>' +
      '</div>';

    const form = $('#modal-form', backdrop);
    options.fields.forEach((field) => {
      const row = document.createElement('div');
      row.className = 'form-row';
      let control;
      if (field.type === 'select') {
        control = '<select class="select" name="' + field.key + '">' +
          field.options.map((opt) => '<option value="' + esc(opt.value) + '"' +
            (String(field.value) === String(opt.value) ? ' selected' : '') + '>' + esc(opt.label) + '</option>').join('') +
          '</select>';
      } else if (field.type === 'textarea') {
        control = '<textarea class="input" name="' + field.key + '" rows="2" placeholder="' + esc(field.placeholder || '') + '">' + esc(field.value || '') + '</textarea>';
      } else {
        control = '<input class="input" name="' + field.key + '" value="' + esc(field.value == null ? '' : field.value) +
          '" placeholder="' + esc(field.placeholder || '') + '" />';
      }
      row.innerHTML = '<label>' + esc(field.label) + '</label>' + control;
      form.appendChild(row);
    });

    document.body.appendChild(backdrop);
    const close = () => backdrop.remove();
    $('#modal-cancel', backdrop).addEventListener('click', close);
    backdrop.addEventListener('click', (event) => { if (event.target === backdrop) close(); });
    backdrop.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });

    $('#modal-submit', backdrop).addEventListener('click', async () => {
      const values = {};
      $$('[name]', form).forEach((input) => { values[input.name] = input.value.trim(); });
      try {
        await options.onSubmit(values, close);
      } catch (error) {
        toast(error.message, 'error');
      }
    });

    const firstInput = $('input, select, textarea', form);
    if (firstInput) firstInput.focus();
  }

  function openTaskModal(task) {
    const isEdit = !!task;
    openModal({
      title: isEdit ? '编辑任务 #' + task.id : '新建任务',
      hint: isEdit ? '截止时间支持「明天下午3点」「下周一」等中文口语' : '标题必填；截止时间可留空表示无期限',
      submitText: isEdit ? '保存修改' : '创建任务',
      twoColumn: true,
      fields: [
        { key: 'title', label: '标题', value: isEdit ? task.title : '', placeholder: '例如：提交软件工程作业文档' },
        { key: 'due_date', label: '截止时间（可留空）', value: isEdit && task.due_date ? task.due_date : '', placeholder: '明天下午3点' },
        { key: 'priority', label: '优先级', type: 'select', value: isEdit ? task.priority : 'medium',
          options: [{ value: 'high', label: '高' }, { value: 'medium', label: '中' }, { value: 'low', label: '低' }] },
        { key: 'status', label: '状态', type: 'select', value: isEdit ? task.status : 'todo',
          options: [{ value: 'todo', label: '待办' }, { value: 'doing', label: '进行中' }, { value: 'done', label: '已完成' }] },
        { key: 'estimated_minutes', label: '预计耗时（分钟）', value: isEdit ? task.estimated_minutes : 30 },
        { key: 'tags', label: '标签（逗号分隔）', value: isEdit ? (task.tags || []).join(',') : '', placeholder: '学习,作业' },
        { key: 'notes', label: '备注', type: 'textarea', value: isEdit ? task.notes : '', placeholder: '地点、参与人、材料…' },
      ],
      onSubmit: async (values, close) => {
        if (!values.title) { toast('标题不能为空', 'error'); return; }
        const payload = {
          title: values.title,
          priority: values.priority,
          status: values.status,
          estimated_minutes: Number(values.estimated_minutes) || 0,
          tags: values.tags ? values.tags.split(/[,，、]/).map((t) => t.trim()).filter(Boolean) : [],
          notes: values.notes || '',
        };
        const rawDue = values.due_date || '';
        if (isEdit) {
          // 用户清空时间输入 => 清除截止时间
          if (!rawDue) payload.clear_due_date = true;
          else payload.due_date = rawDue;
          await api.patch('/api/tasks/' + task.id, payload);
          toast('任务已更新', 'success');
        } else {
          if (rawDue) payload.due_date = rawDue;
          await api.post('/api/tasks', payload);
          toast('任务已创建', 'success');
        }
        close();
        loadTasks();
        refreshSidePanels();
      },
    });
  }

  async function completeTask(id) {
    try {
      await api.post('/api/tasks/' + id + '/complete');
      const row = $('.task-row[data-row="' + id + '"]');
      if (row) row.classList.add('just-done');
      toast('已完成，辛苦啦', 'success');
      loadTasks();
      refreshSidePanels();
    } catch (error) {
      toast('操作失败：' + error.message, 'error');
    }
  }

  function deleteTask(task) {
    openModal({
      title: '删除任务 #' + task.id,
      hint: '删除后不可恢复。与 Agent 的「二次确认」策略保持一致。',
      submitText: '确认删除',
      fields: [{ key: 'confirm', label: '输入任务编号确认', placeholder: String(task.id) }],
      onSubmit: async (values, close) => {
        if (String(values.confirm) !== String(task.id)) { toast('请输入正确的任务编号', 'error'); return; }
        await api.del('/api/tasks/' + task.id);
        toast('已删除', 'success');
        close();
        loadTasks();
        refreshSidePanels();
      },
    });
  }

  /* ----------------------------- 设置与关于 ----------------------------- */
  async function loadMeta() {
    try {
      const data = await api.get('/api/meta');
      state.meta = data;

      $('#meta-grid').innerHTML = [
        ['模型', data.model],
        ['服务地址', data.base_url],
        ['API Key', data.has_api_key ? '已配置 ✓' : '未配置（请编辑 .env）'],
        ['数据库', data.db_path],
        ['MCP Server', (data.mcp_server && data.mcp_server.name) || 'todo-assistant'],
        ['工具数量', String((data.tools || []).length)],
      ].map(([key, value]) => '<div class="kv"><span>' + esc(key) + '</span><strong>' + esc(value) + '</strong></div>').join('');

      $('#tool-count').textContent = (data.tools || []).length;
      $('#tool-rows').innerHTML = (data.tools || []).map((tool, index) => (
        '<tr>' +
          '<td class="faint">' + (index + 1) + '</td>' +
          '<td><code>' + esc(tool.name) + '</code></td>' +
          '<td>' + esc(tool.description) + '</td>' +
          '<td><div class="param-list">' + (tool.parameters || []).map((p) => '<span>' + esc(p) + '</span>').join('') + '</div></td>' +
        '</tr>'
      )).join('') || '<tr><td colspan="4" class="muted">未能获取工具清单（Agent 未就绪？）</td></tr>';

      $('#sb-model').textContent = data.model;
      $('#sb-tools').textContent = (data.tools || []).length;

      const prefs = await api.get('/api/preferences');
      const entries = Object.entries(prefs.preferences || {});
      $('#pref-list').innerHTML = entries.length
        ? entries.map(([key, value]) => '<div class="pref-item"><code>' + esc(key) + '</code> = ' + esc(value) + '</div>').join('')
        : '<span class="muted">暂无偏好记录。试着在对话里说「以后问天气默认用杭州」，之后点「查天气」就会直接查这个城市。</span>';
    } catch (error) {
      toast('加载元信息失败：' + error.message, 'error');
    }
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  /** 检查后端状态，返回 'ok' | 'starting' | 'down'。 */
  async function checkHealth() {
    const conn = $('#conn');
    const text = $('#conn-text');
    try {
      const data = await api.get('/api/health');
      if (data.agent_ready) {
        conn.className = 'conn is-online';
        text.textContent = 'MCP 已连接';
        $('#sb-status').textContent = '就绪';
        return 'ok';
      }
      if (data.starting) {
        conn.className = 'conn';
        text.textContent = '启动中…';
        $('#sb-status').textContent = 'MCP Server 启动中';
        return 'starting';
      }
      conn.className = 'conn is-offline';
      text.textContent = 'Agent 未就绪';
      $('#sb-status').textContent = data.error || 'Agent 未就绪';
      return 'down';
    } catch (error) {
      conn.className = 'conn is-offline';
      text.textContent = '服务不可用';
      $('#sb-status').textContent = '无法连接后端';
      return 'down';
    }
  }

  /** MCP 子进程冷启动需要数秒，这里轮询等待就绪后补齐数据。 */
  async function waitForAgent(maxTries) {
    let status = await checkHealth();
    if (status === 'ok') return true;
    for (let i = 0; i < (maxTries || 20); i++) {
      await sleep(1500);
      status = await checkHealth();
      if (status === 'ok') return true;
      if (status === 'down' && i > 3) return false;
    }
    return false;
  }

  /* ----------------------------- 侧栏联动 ----------------------------- */
  async function refreshSidePanels() {
    try {
      const data = await api.get('/api/tasks/today');
      state.todayDate = data.date;

      const today = data.due_today || [];
      const done = today.filter((t) => t.status === 'done').length;
      const total = today.length;
      const ratio = total ? done / total : 0;

      const circumference = 2 * Math.PI * 32;
      $('#ring-value').setAttribute('stroke-dashoffset', String(circumference * (1 - ratio)));
      $('#ring-text').textContent = Math.round(ratio * 100) + '%';
      $('#today-done').textContent = done + '/' + total;
      $('#overdue-hint').textContent = (data.overdue || []).length
        ? '⚠ ' + data.overdue.length + ' 条已逾期'
        : '暂无逾期';

      const openTasks = today.filter((t) => t.status !== 'done').concat(data.overdue || []);
      const unique = [];
      const seen = new Set();
      openTasks.forEach((t) => { if (!seen.has(t.id)) { seen.add(t.id); unique.push(t); } });

      $('#today-count').textContent = unique.length;
      $('#mini-list').innerHTML = unique.length
        ? unique.slice(0, 8).map((task) => (
            '<li class="mini-item' + (task.status === 'done' ? ' is-done' : '') + '" data-id="' + task.id + '">' +
              '<i class="mini-check">✓</i>' +
              '<div class="mini-body">' +
                '<div class="mini-title">#' + task.id + ' ' + esc(task.title) + '</div>' +
                '<div class="mini-meta">' +
                  '<span>' + esc(task.priority_label) + '</span>' +
                  (task.due_date ? '<span class="' + (task.overdue ? 'late' : '') + '">' + esc(task.due_human) + '</span>' : '<span>无期限</span>') +
                '</div>' +
              '</div>' +
            '</li>'
          )).join('')
        : '<li class="empty"><strong>今天没有到期任务</strong>可以安排点长期目标</li>';
    } catch (error) {
      // 后端未就绪时静默处理，状态栏已经给出提示
    }
  }

  /* ----------------------------- 事件绑定 ----------------------------- */
  function autoGrow(textarea) {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(140, textarea.scrollHeight) + 'px';
  }

  function bindEvents() {
    // 导航
    $('#nav').addEventListener('click', (event) => {
      const btn = event.target.closest('.nav-item');
      if (btn) switchView(btn.dataset.view);
    });

    $('#nav-toggle').addEventListener('click', () => $('#app').classList.toggle('nav-open'));
    $('#theme-toggle').addEventListener('click', () => applyTheme(state.theme === 'dark' ? 'light' : 'dark'));
    $('#refresh').addEventListener('click', async () => {
      const btn = $('#refresh');
      btn.classList.add('is-spinning');
      await Promise.all([refreshSidePanels(), loadMeta().catch(() => {}), state.view === 'tasks' ? loadTasks() : Promise.resolve()]);
      setTimeout(() => btn.classList.remove('is-spinning'), 500);
      toast('数据已刷新');
    });

    $('#new-session').addEventListener('click', () => {
      state.sessionId = 'web-' + Math.random().toString(36).slice(2, 10);
      localStorage.setItem('todo_session', state.sessionId);
      updateSessionBadge();
      messagesEl().innerHTML = '';
      messagesEl().appendChild(messageTemplate('assistant',
        '<div class="msg-bubble"><strong>已开启新会话</strong><p>之前的多轮记忆已隔离，我们可以从零开始聊。</p></div>'));
      toast('已开启新会话：' + state.sessionId);
    });

    // 对话输入
    const input = $('#input');
    input.addEventListener('input', () => autoGrow(input));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendMessage(input.value);
      }
    });
    $('#send').addEventListener('click', () => sendMessage(input.value));
    // 快捷按钮：带 data-action 的是"直接动作"（不经大模型），其余仍是发一句话给 Agent
    $('#chips').addEventListener('click', (event) => {
      const chip = event.target.closest('.chip');
      if (!chip || chip.disabled) return;
      switchView('chat');
      if (chip.dataset.action === 'weather') {
        runWeatherChip(chip);
        return;
      }
      sendMessage(chip.dataset.prompt);
    });

    // 迷你任务点击 -> 向 Agent 追问
    $('#mini-list').addEventListener('click', (event) => {
      const item = event.target.closest('.mini-item');
      if (!item) return;
      const id = item.dataset.id;
      const title = $('.mini-title', item).textContent.replace(/^#\d+\s*/, '');
      switchView('chat');
      sendMessage('第 ' + id + ' 条任务「' + title + '」现在是什么情况？如果要继续做，帮我把状态改成进行中。');
    });

    // 今日计划
    $('#day-prev').addEventListener('click', () => loadToday(shiftDate(-1)));
    $('#day-next').addEventListener('click', () => loadToday(shiftDate(1)));
    $('#day-today').addEventListener('click', () => loadToday(shiftDate(0)));
    $('#day-replan').addEventListener('click', () => {
      switchView('chat');
      sendMessage('帮我把 ' + (state.todayDate || '今天') + ' 的任务重新排一下日程，上午9点开始，下午6点结束。');
    });

    // 任务总览
    $('#f-status').addEventListener('change', (e) => { state.filters.status = e.target.value; loadTasks(); });
    $('#f-priority').addEventListener('change', (e) => { state.filters.priority = e.target.value; loadTasks(); });
    $('#f-tag').addEventListener('change', (e) => { state.filters.tag = e.target.value; loadTasks(); });
    $('#f-overdue').addEventListener('change', (e) => { state.filters.only_overdue = e.target.checked; loadTasks(); });
    let keywordTimer = null;
    $('#f-keyword').addEventListener('input', (e) => {
      clearTimeout(keywordTimer);
      keywordTimer = setTimeout(() => { state.filters.keyword = e.target.value; loadTasks(); }, 320);
    });
    $('#view-toggle').addEventListener('click', (event) => {
      const btn = event.target.closest('button');
      if (!btn) return;
      state.layout = btn.dataset.layout;
      $$('#view-toggle button').forEach((b) => b.classList.toggle('is-active', b === btn));
      renderTaskLists();
    });
    $('#add-task').addEventListener('click', () => openTaskModal(null));

    $('#task-container').addEventListener('click', (event) => {
      const target = event.target.closest('[data-action]');
      if (!target) return;
      const id = Number(target.dataset.id);
      const task = state.tasks.find((t) => t.id === id);
      if (!task) return;
      const action = target.dataset.action;
      if (action === 'complete') completeTask(id);
      else if (action === 'edit') openTaskModal(task);
      else if (action === 'delete') deleteTask(task);
    });

    // 设置页
    $('#set-theme').addEventListener('click', () => applyTheme(state.theme === 'dark' ? 'light' : 'dark'));
    $('#export-checklist').addEventListener('click', () => {
      switchView('chat');
      sendMessage('把今天的待办清单导出成文件，并告诉我文件路径。');
    });
    $('#clear-session').addEventListener('click', async () => {
      if (!state.sessionId) { toast('当前没有会话'); return; }
      try {
        await api.del('/api/sessions/' + encodeURIComponent(state.sessionId));
      } catch (error) { /* 会话可能本就不存在 */ }
      state.sessionId = 'web-' + Math.random().toString(36).slice(2, 10);
      localStorage.setItem('todo_session', state.sessionId);
      updateSessionBadge();
      messagesEl().innerHTML = '';
      toast('会话记忆已清空', 'success');
    });
  }

  /* ----------------------------- 初始化 ----------------------------- */
  function tickClock() {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    $('#sb-time').textContent = now.getFullYear() + '-' + pad(now.getMonth() + 1) + '-' + pad(now.getDate()) +
      ' ' + pad(now.getHours()) + ':' + pad(now.getMinutes());
  }

  async function init() {
    applyTheme(state.theme);
    updateSessionBadge();
    bindEvents();
    tickClock();
    setInterval(tickClock, 30000);

    if (window.innerWidth <= 768) $('#nav-toggle').style.display = '';

    // 把内核能力暴露给 voice.js，避免语音模块重复实现接口调用与渲染逻辑
    window.TodoApp = {
      state,
      api,
      toast,
      esc,
      mdToHtml,
      sleep,
      messagesEl,
      scrollToBottom,
      sendMessage,
      switchView,
      loadMeta,
      currentSessionId,
      autoGrow,
    };
    document.dispatchEvent(new CustomEvent('todoapp:ready'));

    await restoreHistory();
    const ready = await waitForAgent(20);
    if (ready) {
      await Promise.all([refreshSidePanels(), loadMeta().catch(() => {})]);
    } else {
      await refreshSidePanels().catch(() => {});
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
