function pad(n) { return String(n).padStart(2, '0'); }
function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || isNaN(seconds)) return 'unknown';
  seconds = Math.max(0, Math.floor(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}
function parsePrediction(el) {
  try { return JSON.parse(el.dataset.prediction || '{}'); } catch { return {}; }
}
function formatUtcParts(value) {
  const d = new Date(value || '');
  if (isNaN(d.getTime())) return null;
  return {
    date: `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`,
    time: `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`,
  };
}
function setCountdownInterval(el, eventName, seconds, targetUtc) {
  const parts = formatUtcParts(targetUtc);
  el.replaceChildren();
  const main = document.createElement('div');
  main.className = 'countdown-main';
  main.textContent = `Next ${eventName} in: ${fmtDuration(seconds)}`;
  el.appendChild(main);
  if (parts) {
    const date = document.createElement('div');
    date.className = 'countdown-date';
    date.textContent = parts.date;
    const time = document.createElement('div');
    time.className = 'countdown-time';
    time.textContent = parts.time;
    el.appendChild(date);
    el.appendChild(time);
  }
}
function setCountdownMessage(el, message) {
  el.replaceChildren();
  const text = String(message || '').trim();
  if (!text) {
    el.textContent = 'Next transition unknown';
    return;
  }
  const parts = text.split(';').map((part) => part.trim()).filter(Boolean);
  const main = document.createElement('div');
  main.className = 'countdown-main';
  main.textContent = parts.shift() || text;
  el.appendChild(main);
  if (parts.length) {
    const detail = document.createElement('div');
    detail.className = 'countdown-detail';
    detail.textContent = parts.join('; ');
    el.appendChild(detail);
  }
}
function updateCountdown() {
  const el = document.getElementById('countdown');
  if (!el) return;
  const p = parsePrediction(el);
  const state = p.centre_state || '';
  const eventName = state === 'DAY' ? 'sunset' : 'sunrise';
  const base = state === 'DAY' ? p.next_sunset_seconds : p.next_sunrise_seconds;
  const targetUtc = state === 'DAY' ? p.next_sunset_utc : p.next_sunrise_utc;
  if (base === null || base === undefined) {
    setCountdownMessage(el, p.horizon_message || `Next ${eventName}: unknown`);
    return;
  }
  const started = Date.now();
  function tick() {
    const elapsed = (Date.now() - started) / 1000;
    setCountdownInterval(el, eventName, base - elapsed, targetUtc);
  }
  tick();
  setInterval(tick, 1000);
}
function drawSunCanvas() {
  const canvas = document.getElementById('sunCanvas');
  if (!canvas) return;
  const p = parsePrediction(canvas);
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const left = 56, right = w - 24, top = 28, bottom = h - 52;
  const chartH = bottom - top;
  const alt = Math.max(-90, Math.min(90, Number(p.sun_altitude_deg || 0)));
  const yForAlt = (a) => bottom - ((a + 90) / 180) * chartH;
  const horizonY = yForAlt(0);

  // background: sky above horizon, dark below horizon
  const sky = ctx.createLinearGradient(0, top, 0, horizonY);
  sky.addColorStop(0, '#24466d');
  sky.addColorStop(1, '#f7c96a');
  ctx.fillStyle = sky;
  ctx.fillRect(left, top, right - left, Math.max(1, horizonY - top));
  const night = ctx.createLinearGradient(0, horizonY, 0, bottom);
  night.addColorStop(0, '#172235');
  night.addColorStop(1, '#070b12');
  ctx.fillStyle = night;
  ctx.fillRect(left, horizonY, right - left, Math.max(1, bottom - horizonY));

  // grid and labels
  ctx.strokeStyle = 'rgba(255,255,255,0.16)';
  ctx.lineWidth = 1;
  ctx.fillStyle = '#9fb0c8';
  ctx.font = '12px system-ui, sans-serif';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const mark of [90, 45, 0, -45, -90]) {
    const y = yForAlt(mark);
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
    ctx.fillText(`${mark > 0 ? '+' : ''}${mark}°`, left - 8, y);
  }

  // horizon line
  ctx.strokeStyle = '#ffffff';
  ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(left, horizonY); ctx.lineTo(right, horizonY); ctx.stroke();
  ctx.fillStyle = '#e8eef8';
  ctx.textAlign = 'left';
  ctx.fillText('horizon', right - 70, horizonY - 12);

  // altitude curve placeholder line (vertical rail)
  const x = left + (right - left) * 0.55;
  ctx.strokeStyle = 'rgba(255,255,255,0.35)';
  ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();

  // sun marker
  const y = yForAlt(alt);
  const sunGrad = ctx.createRadialGradient(x, y, 2, x, y, 18);
  sunGrad.addColorStop(0, '#fff8bf');
  sunGrad.addColorStop(0.55, '#ffd37c');
  sunGrad.addColorStop(1, 'rgba(255,211,124,0.05)');
  ctx.fillStyle = sunGrad;
  ctx.beginPath(); ctx.arc(x, y, 18, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = '#fff8bf';
  ctx.beginPath(); ctx.arc(x, y, 8, 0, Math.PI * 2); ctx.fill();

  // rising/falling indicator next to the sun
  const trend = String(p.sun_altitude_trend || '').toLowerCase();
  let arrow = '→';
  let dy = 0;
  if (trend.includes('rising')) { arrow = '↑'; dy = -22; }
  else if (trend.includes('falling')) { arrow = '↓'; dy = 22; }
  ctx.strokeStyle = trend.includes('falling') ? '#ffcf85' : '#92f0b1';
  ctx.fillStyle = ctx.strokeStyle;
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.moveTo(x + 46, y - dy * 0.35);
  ctx.lineTo(x + 46, y + dy * 0.65);
  ctx.stroke();
  ctx.font = '24px system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(arrow, x + 46, y + dy * 0.75);

}

document.addEventListener('DOMContentLoaded', () => {
  updateCountdown();
  drawSunCanvas();
});

function setupObserverNameMemory() {
  const key = 'elite_daynight_observer_name';
  const inputs = document.querySelectorAll('[data-observer-name-memory="1"]');
  inputs.forEach((input) => {
    try {
      const saved = localStorage.getItem(key);
      if (saved && !input.value.trim()) input.value = saved;
      function save() {
        const value = input.value.trim();
        if (value) localStorage.setItem(key, value);
        else localStorage.removeItem(key);
      }
      input.addEventListener('input', save);
      input.addEventListener('change', save);
      const form = input.closest('form');
      if (form) form.addEventListener('submit', save);
    } catch {
      // Browsers can disable localStorage; the form still works normally.
    }
  });
}

function formatUtcNowForInput() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}
function setupLiveTimeInput(inputId, checkId) {
  const input = document.getElementById(inputId);
  const check = document.getElementById(checkId);
  if (!input || !check) return;
  let timer = null;
  function updatePredictionLinks() {
    if (checkId !== 'live-prediction-time') return;
    document.querySelectorAll('[data-live-prediction-link="1"]').forEach((link) => {
      try {
        const url = new URL(link.href, window.location.href);
        url.searchParams.set('time', input.value);
        url.searchParams.set('live_prediction_time', check.checked ? '1' : '0');
        link.href = url.toString();
      } catch {
        // Keep the original link if URL parsing fails.
      }
    });
  }
  function applyState() {
    if (check.checked) {
      input.readOnly = true;
      input.value = formatUtcNowForInput();
      updatePredictionLinks();
      if (!timer) {
        timer = setInterval(() => {
          input.value = formatUtcNowForInput();
          updatePredictionLinks();
        }, 1000);
      }
    } else {
      input.readOnly = false;
      if (timer) { clearInterval(timer); timer = null; }
      updatePredictionLinks();
    }
  }
  check.addEventListener('change', applyState);
  input.addEventListener('input', updatePredictionLinks);
  input.addEventListener('change', updatePredictionLinks);
  applyState();
}
function setupLiveObservationTime() {
  setupLiveTimeInput('observation-time', 'live-observation-time');
}
function setupLivePredictionTime() {
  setupLiveTimeInput('prediction-time', 'live-prediction-time');
}

document.addEventListener('DOMContentLoaded', () => {
  setupObserverNameMemory();
  setupLiveObservationTime();
  setupLivePredictionTime();
});

function initSystemAutocomplete() {
  const inputs = document.querySelectorAll('[data-system-autocomplete="1"]');
  inputs.forEach((input) => {
    const wrap = input.closest('.autocomplete-wrap') || input.parentElement;
    const menu = wrap ? wrap.querySelector('.autocomplete-menu') : null;
    if (!menu) return;
    let timer = null;
    let lastQuery = '';

    function hide() {
      menu.hidden = true;
      menu.innerHTML = '';
    }
    function show(items) {
      menu.innerHTML = '';
      if (!items || !items.length) {
        hide();
        return;
      }
      for (const item of items) {
        const a = document.createElement('a');
        a.href = item.url || `/systems/${item.id}/open`;
        a.className = 'autocomplete-item';
        const title = document.createElement('strong');
        title.textContent = item.name || 'Unknown system';
        const detail = document.createElement('span');
        const parts = [];
        if (item.tracked_body_count) parts.push(`${item.tracked_body_count} tracked`);
        if (item.observed_body_count) parts.push(`${item.observed_body_count} observed`);
        if (item.approved_model_count) parts.push(`${item.approved_model_count} model${item.approved_model_count === 1 ? '' : 's'}`);
        detail.textContent = parts.length ? parts.join(' · ') : 'no tracked bodies yet';
        a.appendChild(title);
        a.appendChild(detail);
        menu.appendChild(a);
      }
      menu.hidden = false;
    }
    async function search() {
      const q = input.value.trim();
      if (q.length < 2) {
        hide();
        return;
      }
      if (q === lastQuery) return;
      lastQuery = q;
      try {
        const res = await fetch(`/systems/autocomplete?q=${encodeURIComponent(q)}`);
        if (!res.ok) return;
        const data = await res.json();
        show(data.results || []);
      } catch {
        hide();
      }
    }
    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(search, 180);
    });
    input.addEventListener('focus', search);
    document.addEventListener('click', (ev) => {
      if (!wrap.contains(ev.target)) hide();
    });
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initSystemAutocomplete);
} else {
  initSystemAutocomplete();
}
