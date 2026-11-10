/* AI 聊天组件: 独立页 (/ai) 与中控抽屉共用
   用法: AIChat.init(rootEl, { compact: true|false })
   依赖: common.css 的变量体系; 后端 /api/ai/chat (SSE) */
const AIChat = (() => {
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  // 极简 markdown: 粗体 / 行内代码 / 代码块 / 标题 / 列表 / 换行
  function mdLite(s) {
    let out = esc(s);
    out = out.replace(/```([\s\S]*?)```/g, (_, c) =>
      '<pre class="ai-code">' + c.replace(/^\n+|\n+$/g, '') + '</pre>');
    out = out.replace(/`([^`\n]+)`/g, '<code class="ai-inline">$1</code>');
    out = out.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
    out = out.replace(/^### (.+)$/gm, '<div class="ai-h">$1</div>');
    out = out.replace(/^## (.+)$/gm, '<div class="ai-h">$1</div>');
    out = out.replace(/^- (.+)$/gm, '<div class="ai-li">• $1</div>');
    return out.replace(/\n/g, '<br>');
  }

  function init(root, opts = {}) {
    const compact = !!opts.compact;
    root.classList.add('ai-chat', compact ? 'ai-compact' : 'ai-page');
    root.innerHTML = `
      <div class="ai-msgs"></div>
      <div class="ai-input">
        <textarea class="ai-text" rows="${compact ? 1 : 2}" placeholder="给服务器管家发消息… (Enter 发送, Shift+Enter 换行)"></textarea>
        <button class="ai-send btn-ai" title="发送"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg></button>
        <button class="ai-stop btn-ai ai-hidden" title="停止"><svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg></button>
      </div>`;
    const msgs = root.querySelector('.ai-msgs');
    const text = root.querySelector('.ai-text');
    const sendBtn = root.querySelector('.ai-send');
    const stopBtn = root.querySelector('.ai-stop');
    let running = false;
    let aborter = null;

    const scroll = () => { msgs.scrollTop = msgs.scrollHeight; };

    function addUser(text) {
      const d = document.createElement('div');
      d.className = 'ai-row ai-me';
      d.innerHTML = '<div class="ai-bubble">' + mdLite(text) + '</div>';
      msgs.appendChild(d); scroll();
    }
    function addAssistant() {
      const d = document.createElement('div');
      d.className = 'ai-row ai-bot';
      d.innerHTML = '<div class="ai-bubble"><span class="ai-wait">思考中…</span></div>';
      msgs.appendChild(d); scroll();
      return d.querySelector('.ai-bubble');
    }
    function addStep(text) {
      let steps = msgs.querySelector('.ai-steps:last-of-type');
      if (!steps || steps.dataset.done) {
        steps = document.createElement('div');
        steps.className = 'ai-steps';
        msgs.appendChild(steps);
      }
      const s = document.createElement('div');
      s.className = 'ai-step';
      s.innerHTML = '<span class="ai-dot"></span>' + esc(text);
      steps.appendChild(s); scroll();
      return s;
    }
    function addError(text) {
      const d = document.createElement('div');
      d.className = 'ai-row ai-err';
      d.innerHTML = '<div class="ai-bubble">' + esc(text) + '</div>';
      msgs.appendChild(d); scroll();
    }
    function setRunning(v) {
      running = v;
      sendBtn.classList.toggle('ai-hidden', v);
      stopBtn.classList.toggle('ai-hidden', !v);
      text.disabled = false;
    }

    async function send() {
      const msg = text.value.trim();
      if (!msg || running) return;
      text.value = '';
      addUser(msg);
      setRunning(true);
      aborter = new AbortController();
      let bubble = null;
      let gotFinal = false;
      try {
        const resp = await fetch('/api/ai/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: msg }),
          signal: aborter.signal
        });
        if (!resp.ok) {
          let detail = resp.statusText;
          try { detail = (await resp.json()).detail || detail; } catch (e) {}
          throw new Error(detail);
        }
        bubble = addAssistant();
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf('\n\n')) >= 0) {
            const frame = buf.slice(0, i); buf = buf.slice(i + 2);
            const line = frame.split('\n').find(l => l.startsWith('data: '));
            if (!line) continue;
            const data = line.slice(6);
            if (data === '[DONE]') { gotFinal = true; continue; }
            let ev; try { ev = JSON.parse(data); } catch (e) { continue; }
            if (ev.type === 'status') {
              const names = { turn_start: '开始处理…', step_start: '执行操作…', step_end: '完成一步', turn_end: '本轮结束' };
              addStep(names[ev.phase] || ev.phase);
              if (bubble) { const w = bubble.querySelector('.ai-wait'); if (w) w.textContent = '执行中…'; }
            } else if (ev.type === 'tool_call') {
              const inp = ev.input || {};
              addStep('工具 ' + (ev.tool || '') + (inp.description ? '：' + inp.description : ''));
              if (bubble) { const w = bubble.querySelector('.ai-wait'); if (w) w.textContent = '正在执行工具…'; }
            } else if (ev.type === 'thinking') {
              if (bubble) { const w = bubble.querySelector('.ai-wait'); if (w) w.textContent = '推理中…'; }
            } else if (ev.type === 'text') {
              if (!bubble) bubble = addAssistant();
              const w = bubble.querySelector('.ai-wait'); if (w) w.remove();
              bubble.innerHTML += mdLite(ev.text || '');
              scroll();
            } else if (ev.type === 'final') {
              if (bubble) { bubble.innerHTML = mdLite(ev.text || '(空回复)'); scroll(); }
              gotFinal = true;
            } else if (ev.type === 'error') {
              if (bubble) bubble.remove();
              addError(ev.error || '未知错误');
            }
          }
        }
        if (bubble && !bubble.querySelector('.ai-wait') === false && !gotFinal) {
          bubble.innerHTML = '<span class="ai-wait">连接中断</span>';
        }
      } catch (e) {
        if (e.name === 'AbortError') {
          if (bubble) bubble.innerHTML = '<span class="ai-wait">已停止</span>';
        } else {
          if (bubble) bubble.remove();
          addError(e.message || '连接失败');
        }
      } finally {
        setRunning(false); aborter = null;
        const last = msgs.querySelector('.ai-bot:last-of-type .ai-wait');
        if (last) last.closest('.ai-bubble').innerHTML = '<span class="ai-wait">(无回复)</span>';
      }
    }

    sendBtn.onclick = send;
    stopBtn.onclick = () => { if (aborter) aborter.abort(); };
    text.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
  }

  return { init, esc, mdLite };
})();
