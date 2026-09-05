(() => {
  'use strict';
  const root = document.getElementById('timeline');
  if (!root) return;
  const plot = document.getElementById('timeline-plot');
  const select = document.getElementById('timeline-plan');
  const allButton = document.getElementById('view-all');
  const planButton = document.getElementById('view-plan');
  const refreshButton = document.getElementById('timeline-refresh');
  const detail = document.getElementById('timeline-detail');
  const status = document.getElementById('timeline-status');
  let data = null, byPlan = false, pinned = null, controller = null, generation = 0;
  const clock = minute => `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(Math.floor(minute % 60)).padStart(2, '0')}`;
  const visiblePoints = () => (data?.points || []).filter(point => !byPlan || String(point.plan_id) === select.value);
  function closeDetail() {
    pinned = null;
    detail.hidden = true;
    plot.querySelectorAll('.timeline-dot').forEach(dot => dot.setAttribute('aria-pressed', 'false'));
  }
  function showDetail(point) {
    detail.replaceChildren();
    const title = document.createElement('strong');
    title.textContent = point.note || '暂无备注';
    const text = document.createElement('span');
    text.textContent = `${point.account} · ${point.plan_name}\n计划时间 ${clock(point.final_minute)} · ${point.status}`;
    detail.append(title, text);
    detail.hidden = false;
  }
  function render() {
    if (!data) return;
    const focused = document.activeElement?.dataset.pointId;
    const scroll = plot.parentElement;
    const wasAtBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 2;
    const points = visiblePoints().slice().sort((a, b) => a.final_minute - b.final_minute || a.id.localeCompare(b.id));
    document.getElementById('timeline-empty').hidden = points.length !== 0;
    document.getElementById('timeline-date').textContent = `${data.date} · ${data.timezone} · ${points.length} 个时间点`;
    const width = plot.clientWidth;
    const laneEnds = [];
    const positions = points.map(point => {
      const x = point.final_minute / 1440 * width;
      let lane = laneEnds.findIndex(end => x - end >= 25);
      if (lane === -1) lane = laneEnds.length;
      laneEnds[lane] = x;
      return {point, lane};
    });
    plot.style.height = `${Math.max(190, laneEnds.length * 26 + 100)}px`;
    const fragment = document.createDocumentFragment();
    const axis = document.createElement('div');
    axis.className = 'axis-line';
    fragment.append(axis);
    for (let hour = 0; hour <= 24; hour += 2) {
      const tick = document.createElement('div');
      tick.className = 'axis-tick';
      tick.style.left = `${hour / 24 * 100}%`;
      const label = document.createElement('span');
      label.textContent = `${String(hour).padStart(2, '0')}:00`;
      tick.append(label);
      fragment.append(tick);
    }
    const current = document.createElement('div');
    current.className = 'current-time';
    current.style.left = `${data.now_minute / 1440 * 100}%`;
    const nowLabel = document.createElement('span');
    nowLabel.textContent = `现在 ${clock(data.now_minute)}`;
    current.append(nowLabel);
    fragment.append(current);
    for (const {point, lane} of positions) {
      const dot = document.createElement('button');
      dot.type = 'button';
      dot.className = `timeline-dot ${point.color}`;
      dot.style.left = `${point.final_minute / 1440 * 100}%`;
      dot.style.bottom = `${51 + lane * 26}px`;
      dot.dataset.pointId = point.id;
      dot.setAttribute('aria-label', `${point.note || '暂无备注'}，${point.account}，${point.plan_name}，${clock(point.final_minute)}，${point.status}`);
      dot.setAttribute('aria-pressed', String(pinned === point.id));
      dot.setAttribute('aria-controls', 'timeline-detail');
      dot.addEventListener('mouseenter', () => { if (!pinned) showDetail(point); });
      dot.addEventListener('mouseleave', () => { if (!pinned && document.activeElement !== dot) detail.hidden = true; });
      dot.addEventListener('focus', () => { if (!pinned) showDetail(point); });
      dot.addEventListener('blur', () => { if (!pinned) detail.hidden = true; });
      dot.addEventListener('click', () => {
        if (pinned === point.id) { closeDetail(); return; }
        closeDetail(); pinned = point.id; dot.setAttribute('aria-pressed', 'true'); showDetail(point);
      });
      fragment.append(dot);
    }
    plot.replaceChildren(fragment);
    if (wasAtBottom) scroll.scrollTop = scroll.scrollHeight;
    if (pinned) {
      const point = points.find(point => point.id === pinned);
      if (point) showDetail(point); else closeDetail();
    } else detail.hidden = true;
    if (focused) Array.from(plot.querySelectorAll('.timeline-dot')).find(dot => dot.dataset.pointId === focused)?.focus({preventScroll: true});
  }
  async function load() {
    const requestId = ++generation;
    controller?.abort(); controller = new AbortController();
    const activeController = controller;
    const timeout = setTimeout(() => activeController.abort(), 15000);
    refreshButton.disabled = true;
    try {
      const url = new URL(root.dataset.url, window.location.origin);
      if (byPlan && select.value) url.searchParams.set('plan_id', select.value);
      const response = await fetch(url, {signal: activeController.signal, credentials: 'same-origin', cache: 'no-store'});
      if (requestId !== generation) return;
      if (response.redirected || response.status === 401) { window.location.assign(root.dataset.login); return; }
      if (response.status === 404 && byPlan) { select.value = ''; await load(); return; }
      if (!response.ok) throw new Error('load failed');
      const incoming = await response.json();
      if (requestId !== generation) return;
      const selected = select.value;
      select.replaceChildren(...incoming.plans.map(plan => {
        const option = document.createElement('option');
        option.value = plan.id; option.textContent = plan.name; return option;
      }));
      if (incoming.plans.some(plan => String(plan.id) === selected)) select.value = selected;
      planButton.disabled = incoming.plans.length === 0;
      data = incoming; render();
      status.textContent = '已更新 · 每 30 秒自动更新';
      status.classList.remove('bad');
    } catch (error) {
      if (requestId !== generation) return;
      status.textContent = '刷新失败，保留上次结果 · 请重试';
      status.classList.add('bad');
    } finally {
      clearTimeout(timeout);
      if (requestId === generation) refreshButton.disabled = false;
    }
  }
  function setView(value) {
    byPlan = value; select.hidden = !value;
    allButton.setAttribute('aria-pressed', String(!value));
    planButton.setAttribute('aria-pressed', String(value));
    closeDetail(); load();
  }
  allButton.addEventListener('click', () => setView(false));
  planButton.addEventListener('click', () => setView(true));
  select.addEventListener('change', () => { closeDetail(); load(); });
  refreshButton.addEventListener('click', load);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeDetail(); });
  document.addEventListener('click', event => { if (!event.target.closest('.timeline-dot, #timeline-detail')) closeDetail(); });
  let lastWidth = 0;
  new ResizeObserver(() => {
    if (plot.clientWidth !== lastWidth) { lastWidth = plot.clientWidth; render(); }
  }).observe(plot);
  setInterval(() => { if (!document.hidden) load(); }, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  load();
})();
