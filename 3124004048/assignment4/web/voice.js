/* ==========================================================================
   待办小助理 · 语音模块
   录音（麦克风）→ 浏览器内转 16kHz 单声道 WAV → 识别 → 填入并发送
   助理回复 → 喇叭按钮手动播放 / 自动朗读
   ========================================================================== */
(function () {
  'use strict';

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  /** 语音模块自身状态（与 app.js 的 state 保持独立，避免互相污染）。 */
  const voice = {
    status: null,
    available: false,
    autoRead: false,          // 自动朗读开关
    mode: 'auto',
    maxSeconds: 15,
    recorder: null,
    stream: null,
    chunks: [],
    timer: null,
    stopWatchdog: null,
    startedAt: 0,
    busy: false,              // 录音或识别进行中
    autoReplay: false,        // 录音结束后是否自动回放自己的录音
    player: null,             // 当前播放的 Audio
    playingBtn: null,
    audioCache: new Map(),    // 文本 -> Blob URL，避免重复请求
    lastRecordingUrl: null,   // 最近一次录音，供用户回放自检
    noteIsReplay: false,      // 当前提示是否由回放写入（用于避免覆盖识别信息）
    el: {},
  };

  const MIME_CANDIDATES = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/ogg;codecs=opus',
    'audio/mp4',
  ];

  /**
   * 录音参数：这三项 WebRTC 音频处理都是为"语音通话"设计的，对语音识别反而有害 ——
   *
   * · echoCancellation（回声消除）：会按参考信号做自适应滤波，在没有回声时也会轻微削掉语音；
   * · noiseSuppression（噪声抑制）：谱减法，最容易吃掉清辅音（s/sh/f/x），
   *   而中文识别恰恰很依赖这些高频信息；
   * · autoGainControl：保留开启，用来把笔记本内置麦的偏低电平拉回正常区间。
   *
   * 识别要的是"尽量原始"的音频，所以前两项显式关掉。
   */
  const AUDIO_CONSTRAINTS = {
    channelCount: 1,
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: true,
  };

  /* ---------------------------------------------------------------- 工具 */

  function app() {
    return window.TodoApp;
  }

  function toast(message, kind) {
    const api = app();
    if (api) api.toast(message, kind);
  }

  function announce(message) {
    if (voice.el.live) voice.el.live.textContent = message;
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  /* --------------------------------------------------- 音频：转 16k WAV */

  /** 把 Float32 采样封装成经典的 44 字节头 PCM WAV。 */
  function encodeWav(samples, sampleRate) {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const writeText = (offset, text) => {
      for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
    };
    writeText(0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    writeText(8, 'WAVE');
    writeText(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);                      // PCM
    view.setUint16(22, 1, true);                      // 单声道
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);         // byteRate
    view.setUint16(32, 2, true);                      // blockAlign
    view.setUint16(34, 16, true);                     // 16bit
    writeText(36, 'data');
    view.setUint32(40, samples.length * 2, true);
    let offset = 44;
    for (let i = 0; i < samples.length; i += 1) {
      const value = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(offset, value < 0 ? value * 0x8000 : value * 0x7fff, true);
      offset += 2;
    }
    return buffer;
  }

  /**
   * MediaRecorder 产出的是 webm/opus，而 Windows 识别引擎只接受
   * 16kHz / 16bit / 单声道 PCM WAV。这里用浏览器自带的解码与重采样完成转换，
   * 因此服务端与部署环境都不需要安装 ffmpeg。
   */
  async function blobToWav(blob) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) throw new Error('当前浏览器不支持 Web Audio，无法转换录音格式');

    const context = new AudioCtx();
    let decoded;
    try {
      decoded = await context.decodeAudioData(await blob.arrayBuffer());
    } finally {
      if (context.close) await context.close();
    }

    const targetRate = 16000;
    const length = Math.max(1, Math.ceil(decoded.duration * targetRate));
    const offline = new OfflineAudioContext(1, length, targetRate);
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination);
    source.start(0);
    const rendered = await offline.startRendering();
    return new Blob([encodeWav(rendered.getChannelData(0), targetRate)], { type: 'audio/wav' });
  }

  function pickMimeType() {
    if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
    for (const candidate of MIME_CANDIDATES) {
      if (MediaRecorder.isTypeSupported(candidate)) return candidate;
    }
    return '';
  }

  /* --------------------------------------------------------- 录音控制 */

  function setMicState(state, label) {
    const btn = voice.el.mic;
    if (!btn) return;
    btn.classList.toggle('is-recording', state === 'recording');
    btn.classList.toggle('is-busy', state === 'busy');
    btn.disabled = state === 'busy';
    btn.setAttribute('aria-pressed', String(state === 'recording'));
    btn.title = label || (state === 'recording' ? '点击结束录音' : '点击开始说话');
    btn.setAttribute('aria-label', btn.title);
  }

  function updateTimer() {
    if (!voice.el.timer) return;
    const seconds = (Date.now() - voice.startedAt) / 1000;
    const remain = Math.max(0, voice.maxSeconds - seconds);
    const minutes = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    voice.el.timer.textContent = `${minutes}:${String(secs).padStart(2, '0')}`;
    voice.el.timer.classList.toggle('is-warning', remain <= 3);
    if (seconds >= voice.maxSeconds) stopRecording();
  }

  async function startRecording() {
    if (voice.busy) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast('当前浏览器不支持录音（需要 HTTPS 或 localhost 环境）', 'error');
      return;
    }
    stopPlayback();

    try {
      voice.stream = await navigator.mediaDevices.getUserMedia({ audio: AUDIO_CONSTRAINTS });
    } catch (error) {
      const reason = error && error.name === 'NotAllowedError'
        ? '麦克风权限被拒绝，请在浏览器地址栏的权限设置里允许后重试'
        : `无法访问麦克风：${error.message || error}`;
      toast(reason, 'error');
      announce(reason);
      return;
    }

    const mimeType = pickMimeType();
    try {
      voice.recorder = mimeType
        ? new MediaRecorder(voice.stream, { mimeType })
        : new MediaRecorder(voice.stream);
    } catch (error) {
      toast(`录音初始化失败：${error.message || error}`, 'error');
      releaseStream();
      return;
    }

    voice.chunks = [];
    voice.recorder.ondataavailable = (event) => {
      if (event.data && event.data.size) voice.chunks.push(event.data);
    };
    // 没有这个回调，录音停止后状态永远不会复位（麦克风也会一直占用）
    voice.recorder.onstop = () => finishRecording();
    voice.recorder.onerror = (event) => {
      toast(`录音出错：${(event.error && event.error.message) || '未知原因'}`, 'error');
      stopRecording();
    };
    voice.recorder.start();

    voice.busy = true;
    voice.startedAt = Date.now();
    setMicState('recording');
    if (voice.el.timer) {
      voice.el.timer.hidden = false;
      voice.el.timer.classList.remove('is-warning');
    }
    voice.timer = setInterval(updateTimer, 100);
    updateTimer();
    announce('正在录音，说完后再点一次麦克风结束');
  }

  function releaseStream() {
    if (voice.stream) {
      voice.stream.getTracks().forEach((track) => track.stop());
      voice.stream = null;
    }
  }

  function stopRecording() {
    if (voice.timer) {
      clearInterval(voice.timer);
      voice.timer = null;
    }
    const recorder = voice.recorder;
    if (!recorder) return;
    if (recorder.state === 'inactive') {
      // 极端情况下 onstop 不会再触发，直接收尾
      finishRecording();
      return;
    }
    try {
      recorder.stop();
    } catch (error) {
      finishRecording();
    }
    // 兜底看门狗：若 2 秒内 onstop 仍未触发，强制收尾，避免卡在录音态
    if (voice.stopWatchdog) clearTimeout(voice.stopWatchdog);
    voice.stopWatchdog = setTimeout(() => {
      if (voice.recorder) finishRecording();
    }, 2000);
  }

  function finishRecording() {
    const recorder = voice.recorder;
    if (!recorder) return;               // 已经收尾过，避免重复处理
    voice.recorder = null;
    if (voice.stopWatchdog) {
      clearTimeout(voice.stopWatchdog);
      voice.stopWatchdog = null;
    }
    if (voice.timer) {
      clearInterval(voice.timer);
      voice.timer = null;
    }

    releaseStream();
    if (voice.el.timer) voice.el.timer.hidden = true;

    const blob = new Blob(voice.chunks, { type: recorder.mimeType || 'audio/webm' });
    voice.chunks = [];

    if (blob.size < 1200) {
      voice.busy = false;
      setMicState('idle');
      toast('录音太短了，请说完整一句话', 'error');
      return;
    }
    recognize(blob);
  }

  async function toggleRecording() {
    if (voice.busy) {
      if (voice.recorder && voice.recorder.state === 'recording') stopRecording();
      return;
    }
    if (voice.recorder && voice.recorder.state === 'recording') {
      stopRecording();
      return;
    }
    await startRecording();
  }

  /* --------------------------------------------------------- 识别与发送 */

  async function recognize(blob) {
    setMicState('busy', '正在识别…');
    announce('正在识别语音');
    const placeholder = voice.el.input ? voice.el.input.placeholder : '';
    if (voice.el.input) voice.el.input.placeholder = '正在识别…';
    const startedAt = Date.now();

    try {
      const wav = await blobToWav(blob);
      keepLastRecording(wav);
      // 先把录音回放出去，再并行做识别。否则"识别→自动发送"一气呵成，
      // 用户根本没机会听到自己刚才说了什么（这正是此前反馈的问题）。
      if (voice.autoReplay) replayLastRecording(true);
      const response = await fetch('/api/voice/transcribe?mode=' + encodeURIComponent(voice.mode), {
        method: 'POST',
        headers: { 'Content-Type': 'audio/wav' },
        body: wav,
      });
      if (!response.ok) {
        let detail = `识别接口返回 ${response.status}`;
        try {
          const payload = await response.json();
          if (payload && payload.detail) detail = payload.detail;
        } catch (error) { /* 忽略解析失败 */ }
        throw new Error(detail);
      }
      const result = await response.json();
      const elapsed = Date.now() - startedAt;

      if (!result.ok) {
        toast(result.error || '没有识别到内容，请重试', 'error');
        announce(result.error || '识别失败');
        return;
      }
      applyResult(result, elapsed);
    } catch (error) {
      toast(`语音识别失败：${error.message || error}`, 'error');
      announce('语音识别失败');
    } finally {
      if (voice.el.input) voice.el.input.placeholder = placeholder;
      voice.busy = false;
      setMicState('idle');
    }
  }

  function applyResult(result, elapsed) {
    const api = app();
    if (!api) return;

    api.switchView('chat');
    if (voice.el.input) {
      voice.el.input.value = result.text;
      api.autoGrow(voice.el.input);
      voice.el.input.focus();
    }
    inputNote(buildNote(result, elapsed));

    announce(`识别结果：${result.text}`);
    warnIfQuiet(result);
    if (result.changed) {
      toast(`已纠错：「${result.raw}」→「${result.text}」`);
    }

    if (voice.status && voice.status.auto_send) {
      // 让输入框里的文字先被用户看到，再自动发送
      setTimeout(() => {
        if (voice.el.input && voice.el.input.value.trim()) api.sendMessage(voice.el.input.value);
      }, 260);
    }
  }

  function buildNote(result, elapsed) {
    const parts = [`识别用时 ${elapsed}ms`];
    if (result.grammar === 'commands') parts.push('命中指令模板');
    else if (result.grammar === 'model') parts.push('本地大模型');
    if (result.confidence) parts.push(`置信度 ${(result.confidence * 100).toFixed(0)}%`);
    const audio = result.audio || {};
    if (typeof audio.peak_dbfs === 'number') parts.push(`录音峰值 ${audio.peak_dbfs}dBFS`);
    if (result.changed) parts.push(`已纠错（原文：${result.raw}）`);
    return parts.join(' · ');
  }

  /** 保留最近一次录音，用户可以自己回放确认"录进去的声音到底什么样"。 */
  function keepLastRecording(wav) {
    if (voice.lastRecordingUrl) URL.revokeObjectURL(voice.lastRecordingUrl);
    voice.lastRecordingUrl = URL.createObjectURL(wav);
    if (voice.el.replay) voice.el.replay.hidden = false;
  }

  /**
   * 回放最近一次录音。
   *
   * Args:
   *   auto: 由「录音结束后自动回放」触发（与手动点击区分，用于文案与按钮态）。
   */
  function replayLastRecording(auto) {
    if (!voice.lastRecordingUrl) {
      if (!auto) toast('还没有可回放的录音，先用麦克风说一句', 'error');
      return;
    }
    // 同一时间只允许一路音频，避免和"朗读回复"叠在一起
    stopPlayback();

    const audio = new Audio(voice.lastRecordingUrl);
    voice.player = audio;
    setReplayState(true, auto);

    const finish = () => {
      if (voice.player !== audio) return;
      voice.player = null;
      setReplayState(false);
    };
    audio.onended = finish;
    audio.onerror = () => {
      finish();
      toast('录音回放失败', 'error');
    };
    audio
      .play()
      .then(() => announce('正在回放你刚才的录音'))
      .catch((error) => {
        finish();
        // 浏览器的自动播放策略会拦截"没有用户手势"的播放
        toast(
          `录音回放被浏览器拦截（${error.message || error}），可点「回放录音」手动播放`,
          'error'
        );
      });
  }

  /** 回放时按钮切到"回放中…"态，并同步输入框上方的提示。 */
  function setReplayState(playing, auto) {
    const button = voice.el.replay;
    if (button) {
      button.classList.toggle('is-playing', playing);
      button.setAttribute('aria-pressed', String(playing));
      const label = $('span', button);
      if (label) label.textContent = playing ? '回放中…' : '回放录音';
      button.title = playing ? '正在回放你刚才的录音' : '回放你刚才的录音，确认录进去的声音是否正常';
    }
    if (playing) {
      inputNote(auto ? '正在回放你刚才的录音…（同时进行识别）' : '正在回放你刚才的录音…');
      voice.noteIsReplay = true;   // 标记提示归回放所有，结束时可以安全清掉
    } else if (voice.noteIsReplay) {
      // 只在提示还停留在"回放中"时清掉，识别信息不会被误伤
      voice.noteIsReplay = false;
      inputNote('');
    }
  }

  /** 音量过低时给出明确提示——这是"识别不准"最常见的非引擎原因。 */
  function warnIfQuiet(result) {
    const audio = result.audio || {};
    if (audio.level_ok === false) {
      toast(
        `录音音量偏低（峰值 ${audio.peak_dbfs}dBFS，正常说话应在 -18dBFS 以上）。` +
        '请靠近麦克风、或调高系统「输入音量」后重试；可点「回放录音」确认实际录到的声音。',
        'error'
      );
    }
  }

  function inputNote(text) {
    const node = voice.el.note;
    // 非回放来源的提示一律接管提示位，避免回放结束时把识别信息覆盖掉
    voice.noteIsReplay = false;
    if (!node) return;
    if (!text) {
      node.hidden = true;
      node.textContent = '';
      return;
    }
    node.hidden = false;
    node.textContent = text;
  }

  /* --------------------------------------------------------------- 播放 */

  function stopPlayback() {
    if (voice.player) {
      try {
        voice.player.pause();
      } catch (error) { /* 忽略 */ }
      voice.player = null;
    }
    if (voice.playingBtn) {
      voice.playingBtn.classList.remove('is-playing');
      voice.playingBtn.setAttribute('aria-pressed', 'false');
      voice.playingBtn = null;
    }
  }

  async function fetchSpeech(text) {
    if (voice.audioCache.has(text)) return voice.audioCache.get(text);
    const response = await fetch('/api/voice/speak', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) {
      let detail = `语音合成失败（${response.status}）`;
      try {
        const payload = await response.json();
        if (payload && payload.detail) detail = payload.detail;
      } catch (error) { /* 忽略 */ }
      throw new Error(detail);
    }
    const url = URL.createObjectURL(await response.blob());
    voice.audioCache.set(text, url);
    // 简单限流：缓存过多时清掉最早的一半，避免内存无限增长
    if (voice.audioCache.size > 40) {
      const keys = Array.from(voice.audioCache.keys()).slice(0, 20);
      keys.forEach((key) => {
        URL.revokeObjectURL(voice.audioCache.get(key));
        voice.audioCache.delete(key);
      });
    }
    return url;
  }

  async function play(text, button) {
    if (!voice.available) {
      toast(voice.status && voice.status.reason ? voice.status.reason : '语音合成不可用', 'error');
      return;
    }
    if (voice.playingBtn === button && voice.player) {
      stopPlayback();
      announce('已停止朗读');
      return;
    }
    stopPlayback();

    if (button) {
      button.classList.add('is-playing');
      button.setAttribute('aria-pressed', 'true');
      button.title = '点击停止';
    }
    voice.playingBtn = button || null;

    try {
      const url = await fetchSpeech(text);
      const audio = new Audio(url);
      voice.player = audio;
      audio.onended = () => {
        if (voice.playingBtn === button) stopPlayback();
      };
      audio.onerror = () => {
        toast('音频播放失败', 'error');
        stopPlayback();
      };
      await audio.play();
      announce('正在朗读回复');
    } catch (error) {
      stopPlayback();
      toast(error.message || '朗读失败', 'error');
    }
  }

  /* ------------------------------------------------------ 喇叭按钮注入 */

  const SPEAK_ICON =
    '<svg class="speak-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4V5Z"/>' +
    '<path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></svg>' +
    '<span class="wave"><i></i><i></i><i></i></span>';

  function injectSpeaker(node) {
    if (!node || node.querySelector('.msg-actions')) return;
    const bubble = node.querySelector('.msg-bubble');
    if (!bubble) return;
    const text = bubble.textContent.trim();
    if (!text) return;

    const actions = document.createElement('div');
    actions.className = 'msg-actions';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'speak-btn';
    button.innerHTML = SPEAK_ICON;
    button.title = '朗读这条回复';
    button.setAttribute('aria-label', '朗读这条回复');
    button.setAttribute('aria-pressed', 'false');
    button.addEventListener('click', () => play(text, button));
    actions.appendChild(button);
    bubble.insertAdjacentElement('afterend', actions);
  }

  function injectAllSpeakers() {
    const api = app();
    if (!api) return;
    $$('.msg-assistant', api.messagesEl()).forEach(injectSpeaker);
  }

  /* ------------------------------------------------------------- 面板 */

  function renderHints(hints) {
    const panel = voice.el.panel;
    if (!panel || !hints || !hints.groups) return;
    panel.innerHTML =
      hints.groups.map((group) => (
        '<div class="hints-group">' +
          '<div class="hints-scene">' + app().esc(group.scene) + '</div>' +
          '<div class="hints-chips">' +
            group.examples.map((example) => (
              '<button type="button" class="hint-chip" data-text="' + app().esc(example) + '">' +
                app().esc(example) +
              '</button>'
            )).join('') +
          '</div>' +
        '</div>'
      )).join('') +
      '<div class="hints-foot">照上面这样说识别最准；识别引擎：' +
        app().esc(hints.engine || '未知') + ' · 模式：' +
        app().esc(hints.mode === 'commands' ? '仅指令' : '自由说话 + 指令') +
      '</div>';

    panel.addEventListener('click', (event) => {
      const chip = event.target.closest('.hint-chip');
      if (!chip) return;
      voice.el.input.value = chip.dataset.text;
      app().autoGrow(voice.el.input);
      voice.el.input.focus();
      toggleHints(false);
      inputNote('已从指令参考填入，按 Enter 发送');
    });
  }

  function toggleHints(force) {
    const panel = voice.el.panel;
    const toggle = voice.el.hintsToggle;
    if (!panel || !toggle) return;
    const open = typeof force === 'boolean' ? force : panel.hidden;
    panel.hidden = !open;
    toggle.classList.toggle('is-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    if (open) inputNote('点击任意例句即可填入输入框');
  }

  const MODEL_STATE_TEXT = {
    not_loaded: '未加载',
    loading: '加载中…',
    ready: '已就绪',
    failed: '加载失败',
  };

  function renderSettings() {
    const meta = voice.el.settingsMeta;
    if (!meta || !voice.status) return;
    const { asr, tts, correction, limits } = voice.status;
    const dot = (ok) => '<i class="voice-dot' + (ok ? ' is-on' : '') + '"></i>';
    const isWhisper = asr.provider === 'whisper';

    const qualityText = !asr.available
      ? '不可用'
      : isWhisper
        ? `高精度 · 本地模型 ${asr.model_size}（${MODEL_STATE_TEXT[asr.model_state] || asr.model_state}）`
        : '基础 · Windows 自带引擎（可安装 faster-whisper 提升精度）';

    const rows = [
      { label: '识别引擎', value: asr.available ? asr.engine : '不可用', ok: asr.available },
      { label: '识别精度', value: qualityText, ok: isWhisper },
      { label: '合成音色', value: tts.available ? `${tts.voice}（${tts.culture}）` : '不可用', ok: tts.available },
      {
        label: '识别纠错',
        value: correction.enabled
          ? (correction.available
              ? '已开启（仅在识别置信度较低时启用，避免改坏正确结果）'
              : '已跳过（未配置模型密钥，直接使用原始识别文本）')
          : '已关闭',
        ok: correction.enabled && correction.available,
      },
      { label: '录音上限', value: `${limits.max_seconds} 秒`, ok: true },
      { label: '朗读上限', value: `${limits.max_chars} 字`, ok: true },
    ];

    if (asr.fallback_reason) {
      rows.push({ label: '引擎回退', value: asr.fallback_reason, ok: false });
    }

    meta.innerHTML = rows.map((item) => (
      '<div class="kv"><span>' + dot(item.ok) + app().esc(item.label) + '</span>' +
      '<strong>' + app().esc(item.value) + '</strong></div>'
    )).join('');

    const badge = voice.el.settingsBadge;
    if (badge) {
      badge.textContent = !voice.status.available
        ? '不可用'
        : (isWhisper ? '离线 · 高精度' : '离线 · 基础精度');
      badge.classList.toggle('badge-hint-off', !voice.status.available);
    }
  }

  function setUnavailable(reason) {
    voice.available = false;
    if (voice.el.mic) voice.el.mic.hidden = true;
    if (voice.el.micFallback) {
      voice.el.micFallback.hidden = false;
      voice.el.micFallback.textContent = `语音输入不可用：${reason}`;
    }
    $$('.speak-btn').forEach((btn) => { btn.hidden = true; });
    if (voice.el.autoRead) voice.el.autoRead.hidden = true;
    if (voice.el.autoReplay) voice.el.autoReplay.hidden = true;
    if (voice.el.hintsToggle) voice.el.hintsToggle.hidden = true;
    if (voice.el.replay) voice.el.replay.hidden = true;
    if (voice.el.panel) voice.el.panel.hidden = true;
    renderSettings();
  }

  /* ------------------------------------------------- 录音回放 / 自动朗读 */

  /**
   * 录音结束后是否自动回放自己的录音。
   *
   * 默认开启：语音输入是"识别→自动发送"一气呵成的，如果不回放，用户就没机会
   * 确认自己刚才到底说了什么，出了问题也无从判断是麦克风还是识别引擎的原因。
   */
  function setAutoReplay(enabled, persist) {
    voice.autoReplay = !!enabled;
    const button = voice.el.autoReplay;
    if (button) {
      button.classList.toggle('is-on', voice.autoReplay);
      button.setAttribute('aria-pressed', String(voice.autoReplay));
      const label = $('.switch-label', button);
      if (label) label.textContent = voice.autoReplay ? '录音后回放已开启' : '录音后回放';
    }
    if (persist) {
      try {
        localStorage.setItem('todo_voice_autoreplay', voice.autoReplay ? '1' : '0');
      } catch (error) { /* 无痕模式忽略 */ }
    }
  }


  function setAutoRead(enabled, persist) {
    voice.autoRead = !!enabled;
    const button = voice.el.autoRead;
    if (button) {
      button.classList.toggle('is-on', voice.autoRead);
      button.setAttribute('aria-pressed', String(voice.autoRead));
      const label = $('.switch-label', button);
      if (label) label.textContent = voice.autoRead ? '自动朗读已开启' : '自动朗读';
    }
    if (persist) {
      try {
        localStorage.setItem('todo_voice_autoread', voice.autoRead ? '1' : '0');
      } catch (error) { /* 无痕模式忽略 */ }
    }
  }

  /* ------------------------------------------------------------- 初始化 */

  function cacheElements() {
    voice.el = {
      mic: $('#voice-mic'),
      micFallback: $('#voice-mic-fallback'),
      timer: $('#voice-timer'),
      note: $('#voice-note'),
      input: $('#input'),
      autoRead: $('#voice-autoread'),
      autoReplay: $('#voice-autoreplay'),
      hintsToggle: $('#voice-hints-toggle'),
      panel: $('#voice-hints-panel'),
      replay: $('#voice-replay'),
      live: $('#voice-live'),
      settingsMeta: $('#voice-meta'),
      settingsBadge: $('#voice-engine-badge'),
      testBtn: $('#voice-test'),
    };
  }

  function bindEvents() {
    if (voice.el.mic) voice.el.mic.addEventListener('click', toggleRecording);
    if (voice.el.hintsToggle) voice.el.hintsToggle.addEventListener('click', () => toggleHints());
    // 注意：必须用箭头函数包一层。直接把 replayLastRecording 交给 addEventListener，
    // 浏览器会把 MouseEvent 当作第一个参数（auto）传进来，而事件对象是真值，
    // 会让"还没有可回放的录音"的提示被静默跳过，表现为点了没有任何反馈。
    if (voice.el.replay) voice.el.replay.addEventListener('click', () => replayLastRecording(false));

    // 打开「设置与关于」时刷新一次状态：whisper 模型是后台加载的，
    // 首次查看时可能还在 loading，过一会儿再看得见"已就绪"。
    document.addEventListener('click', (event) => {
      if (event.target.closest('.nav-item[data-view="settings"]')) refreshStatus();
    });
    if (voice.el.autoReplay) {
      voice.el.autoReplay.addEventListener('click', () => {
        setAutoReplay(!voice.autoReplay, true);
        announce(voice.autoReplay ? '已开启录音后自动回放' : '已关闭录音后自动回放');
      });
    }
    if (voice.el.autoRead) {
      voice.el.autoRead.addEventListener('click', () => {
        setAutoRead(!voice.autoRead, true);
        announce(voice.autoRead ? '已开启自动朗读' : '已关闭自动朗读');
        if (!voice.autoRead) stopPlayback();
      });
    }
    if (voice.el.testBtn) {
      voice.el.testBtn.addEventListener('click', () => {
        play('语音功能正常，以后我就可以把回复念给你听了。', voice.el.testBtn);
      });
    }

    // 助理回复完成 → 注入喇叭按钮 + 可选自动朗读
    document.addEventListener('assistant-final', (event) => {
      const node = event.detail && event.detail.node;
      if (node) injectSpeaker(node);
      if (voice.autoRead && event.detail && event.detail.text) {
        const button = node ? node.querySelector('.speak-btn') : null;
        play(event.detail.text, button);
      }
    });

    // 快捷键：Ctrl/Cmd + Shift + M 开始/结束录音
    document.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'm') {
        event.preventDefault();
        if (voice.available) toggleRecording();
      }
    });

    window.addEventListener('beforeunload', () => {
      stopPlayback();
      voice.audioCache.forEach((url) => URL.revokeObjectURL(url));
      if (voice.lastRecordingUrl) URL.revokeObjectURL(voice.lastRecordingUrl);
    });
  }

  async function loadStatus() {
    try {
      const status = await app().api.get('/api/voice/status');
      voice.status = status;
      voice.mode = status.asr && status.asr.mode ? status.asr.mode : 'auto';
      voice.maxSeconds = (status.limits && status.limits.max_seconds) || 15;

      if (!status.available) {
        setUnavailable(status.reason || '本机缺少中文语音引擎');
        return;
      }
      // 回放只依赖浏览器录音，与识别引擎无关

      voice.available = true;
      if (voice.el.mic) voice.el.mic.hidden = false;
      setMicState('idle');
      renderHints(status.hints);
      renderSettings();

      // 两个开关都优先使用用户上次的本地选择，其次服务端默认值
      let storedRead = null;
      let storedReplay = null;
      try {
        storedRead = localStorage.getItem('todo_voice_autoread');
        storedReplay = localStorage.getItem('todo_voice_autoreplay');
      } catch (error) { /* 无痕模式忽略 */ }
      setAutoRead(storedRead === null ? !!status.auto_read : storedRead === '1', false);
      // 录音后回放默认开启：否则"识别→自动发送"太快，用户听不到自己说了什么
      setAutoReplay(
        storedReplay === null ? status.auto_replay !== false : storedReplay === '1',
        false
      );

      injectAllSpeakers();
      inputNote('');
    } catch (error) {
      setUnavailable(`语音服务未就绪（${error.message}）`);
    }
  }

  async function refreshStatus() {
    if (!voice.available) return;
    try {
      voice.status = await app().api.get('/api/voice/status');
      renderSettings();
    } catch (error) { /* 状态刷新失败不影响使用 */ }
  }

  async function init() {
    cacheElements();
    bindEvents();
    await sleep(0);
    await loadStatus();
  }

  /* --------------------------------------------------------- 调试钩子 */

  // 自动化测试（Playwright）无法真的对着麦克风说话，因此在 localhost 下暴露
  // 内部函数，让测试可以拿一段真实音频走完"转码 → 识别 → 填入 → 发送"的完整链路。
  // 非本机访问时不挂载，避免把内部实现暴露给外部。
  if (location.hostname === 'localhost' || location.hostname === '127.0.0.1') {
    window.__voiceDebug = {
      state: voice,
      encodeWav,
      blobToWav,
      recognize,
      applyResult,
      play,
      stopPlayback,
      injectAllSpeakers,
      inputNote,
    };
  }

  function bootstrap() {
    if (window.TodoApp) {
      init();
      return;
    }
    let done = false;
    const run = () => {
      if (done) return;
      done = true;
      init();
    };
    document.addEventListener('todoapp:ready', run, { once: true });
    // 兜底轮询，避免事件因加载顺序错过
    let tries = 0;
    const poll = setInterval(() => {
      tries += 1;
      if (window.TodoApp) {
        clearInterval(poll);
        run();
      } else if (tries > 40) {
        clearInterval(poll);
      }
    }, 250);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
  } else {
    bootstrap();
  }
})();
