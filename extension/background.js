const ext = globalThis.browser ?? globalThis.chrome;
// Multi-server failover: try each API URL in order; remember the last good one.
const DEFAULTS = { apiUrls: [], apiUrl: "", apiKey: "", nltk: true, lastGood: "" };
const FAST_MS = 30000;   // first pass: fail over quickly
const SLOW_MS = 70000;   // second pass: allow a cold-starting server to wake

class Fatal extends Error {}   // errors that failover cannot fix (bad key, too large)

const clean = (list) =>
  [...new Set(list.map((u) => String(u).trim().replace(/\/+$/, "")).filter(Boolean))];

async function getSettings() {
  const s = { ...DEFAULTS, ...(await ext.storage.local.get(Object.keys(DEFAULTS))) };
  const urls = clean([...(s.apiUrls || []), s.apiUrl || ""]);
  const i = urls.indexOf(s.lastGood);
  if (i > 0) urls.unshift(...urls.splice(i, 1));
  return { ...s, urls };
}

async function post(url, key, body, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const headers = { "Content-Type": "application/json" };
    if (key) headers["X-API-Key"] = key;
    const res = await fetch(url + "/v1/extract", {
      method: "POST", headers, body, signal: ctrl.signal,
    });
    if (res.status === 401) throw new Fatal("Invalid API key.");
    if (res.status === 413) throw new Fatal("Page chunk too large for the API.");
    if (!res.ok) throw new Error(`API error ${res.status}`); // 429/5xx: try next server
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

function describe(e, n) {
  const base = e?.name === "AbortError" ? "API timed out"
    : e instanceof TypeError ? "Cannot reach API"
    : e?.message || "API error";
  return `${base} (tried ${n} server${n === 1 ? "" : "s"}).`;
}

async function callApi(texts) {
  const s = await getSettings();
  if (!s.urls.length) throw new Error("API URL not set. Open Settings.");
  const body = JSON.stringify({ texts, options: { nltk: s.nltk, phrases: s.nltk } });
  // Long payloads need longer: +1 s per 10,000 characters (max +60 s).
  const chars = texts.reduce((n, t) => n + t.length, 0);
  const extra = Math.min(60000, Math.ceil(chars / 10000) * 1000);
  let lastErr;
  for (const timeout of [FAST_MS + extra, SLOW_MS + extra]) {
    for (const url of s.urls) {
      try {
        const data = await post(url, s.apiKey, body, timeout);
        if (url !== s.lastGood) ext.storage.local.set({ lastGood: url });
        return data;
      } catch (e) {
        if (e instanceof Fatal) throw e;
        lastErr = e;
      }
    }
  }
  throw new Error(describe(lastErr, s.urls.length));
}

async function health() {
  const s = await getSettings();
  if (!s.urls.length) throw new Error("API URL not set.");
  return Promise.all(s.urls.map(async (url) => {
    const t0 = Date.now();
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), SLOW_MS);
    try {
      const res = await fetch(url + "/health", { signal: ctrl.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const j = await res.json();
      return { url, ok: true, ms: Date.now() - t0, model: j.model };
    } catch (e) {
      return { url, ok: false, error: e.name === "AbortError" ? "timed out" : e.message };
    } finally {
      clearTimeout(timer);
    }
  }));
}

ext.runtime.onMessage.addListener((msg, _sender, send) => {
  if (msg.type === "extract") {
    callApi(msg.texts).then((data) => send({ ok: true, data }))
      .catch((e) => send({ ok: false, error: e.message }));
    return true;
  }
  if (msg.type === "health") {
    health().then((data) => send({ ok: true, data }))
      .catch((e) => send({ ok: false, error: e.message }));
    return true;
  }
});
