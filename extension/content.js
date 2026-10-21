// Injected on demand (activeTab). Wraps entities in <mark> elements.
// Commands: "highlight" (whole page), "selection" (additive), "clear".
(() => {
  const ext = globalThis.browser ?? globalThis.chrome;
  // Replace any stale instance (for example after an extension reload).
  try { window.__nerHighlighter?.dispose(); } catch (e) { /* old context is dead */ }

  const LABELS = {
    SECURITY: "Security", GEOPOLITICAL: "Geopolitical", ACADEMIC: "Academic",
    DATE: "Date", TIME: "Time", COUNTRY: "Country", INSTITUTION: "Institution",
    PERSON: "Person", LOCATION: "Location", PHRASE: "Phrase",
  };
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA", "INPUT", "SELECT",
    "OPTION", "IFRAME", "SVG", "CANVAS", "CODE", "PRE", "KBD", "MARK"]);
  const MAX_ITEMS = 3000;
  const MAX_TEXT_CHARS = 200000;            // the server splits long texts itself
  const BATCH_ITEMS = 150, BATCH_CHARS = 60000;
  let busy = false;

  // ---------- collecting text ----------
  // An item is one piece of a text node: { node, orig, base, text }.
  // `text` is what is analyzed; `base` is its offset inside `orig` (node value).
  function eligible(node) {
    const p = node.parentElement;
    if (!p) return false;
    if (SKIP.has(p.tagName.toUpperCase()) || p.isContentEditable) return false;
    if (p.closest("#ner-legend, mark.ner-hl")) return false;
    if (p.checkVisibility && !p.checkVisibility()) return false;
    return true;
  }

  function collectPage() {
    const items = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode() && items.length < MAX_ITEMS) {
      const node = walker.currentNode, v = node.nodeValue;
      if (v.trim().length < 3 || v.length > MAX_TEXT_CHARS || !eligible(node)) continue;
      items.push({ node, orig: v, base: 0, text: v });
    }
    return items;
  }

  function textNodesIn(range) {
    const root = range.commonAncestorContainer;
    if (root.nodeType === Node.TEXT_NODE) return [root];
    const nodes = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      if (range.intersectsNode(walker.currentNode)) nodes.push(walker.currentNode);
    }
    return nodes;
  }

  function collectSelection() {
    const sel = window.getSelection();
    const items = [], seen = new Set();
    if (!sel || sel.isCollapsed) return items;
    for (let i = 0; i < sel.rangeCount; i++) {
      const range = sel.getRangeAt(i);
      for (const node of textNodesIn(range)) {
        if (seen.has(node) || !eligible(node)) continue;
        const v = node.nodeValue;
        const start = node === range.startContainer ? range.startOffset : 0;
        const end = node === range.endContainer ? range.endOffset : v.length;
        const text = v.slice(start, end);
        if (text.trim().length < 3 || text.length > MAX_TEXT_CHARS) continue;
        seen.add(node);
        items.push({ node, orig: v, base: start, text });
        if (items.length >= MAX_ITEMS) return items;
      }
    }
    return items;
  }

  // ---------- applying highlights ----------
  function wrap(item, spans) {
    const { node, orig, base } = item;
    if (!node.isConnected || node.nodeValue !== orig) return;   // page changed meanwhile
    let cur = node, consumed = 0;
    for (const s of [...spans].sort((a, b) => a.start - b.start)) {
      const start = s.start + base, end = s.end + base;
      if (!LABELS[s.label] || start < consumed || end > orig.length || end <= start) continue;
      const mid = cur.splitText(start - consumed);
      const after = mid.splitText(end - start);
      const mark = document.createElement("mark");
      mark.className = `ner-hl ner-${s.label}`;
      mark.title = LABELS[s.label];
      mid.parentNode.replaceChild(mark, mid);
      mark.appendChild(mid);
      cur = after;
      consumed = end;
    }
  }

  function countMarks() {
    const counts = {};
    let total = 0;
    document.querySelectorAll("mark.ner-hl").forEach((m) => {
      const label = [...m.classList].find((c) => c.startsWith("ner-") && c !== "ner-hl")?.slice(4);
      if (label) { counts[label] = (counts[label] || 0) + 1; total++; }
    });
    return { counts, total };
  }

  function clear() {
    document.querySelectorAll("mark.ner-hl").forEach((m) => {
      const p = m.parentNode;
      while (m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m);
      p.normalize();
    });
    document.getElementById("ner-legend")?.remove();
    [...document.documentElement.classList].filter((c) => c.startsWith("ner-off-"))
      .forEach((c) => document.documentElement.classList.remove(c));
  }

  function showLegend() {
    const { counts } = countMarks();
    document.getElementById("ner-legend")?.remove();
    const box = document.createElement("div");
    box.id = "ner-legend";
    for (const [label, name] of Object.entries(LABELS)) {
      if (!counts[label]) continue;
      const chip = document.createElement("button");
      chip.className = `ner-chip ner-${label}`;
      chip.textContent = `${name} ${counts[label]}`;
      if (document.documentElement.classList.contains(`ner-off-${label}`)) chip.classList.add("off");
      chip.addEventListener("click", () => {
        const off = document.documentElement.classList.toggle(`ner-off-${label}`);
        chip.classList.toggle("off", off);
      });
      box.appendChild(chip);
    }
    const x = document.createElement("button");
    x.className = "ner-close"; x.textContent = "\u2715"; x.title = "Hide legend";
    x.addEventListener("click", () => box.remove());
    box.appendChild(x);
    document.body.appendChild(box);
  }

  // ---------- talking to the API ----------
  // Ask the background worker; if it is unavailable, call the API directly.
  async function callApi(texts) {
    try {
      const res = await ext.runtime.sendMessage({ type: "extract", texts });
      if (res) return res;
    } catch (e) { /* background not running: fall through */ }
    return directFetch(texts);
  }

  async function directFetch(texts) {
    const s = await ext.storage.local.get({ apiUrls: [], apiUrl: "", apiKey: "", nltk: true });
    const urls = [...new Set([...(s.apiUrls || []), s.apiUrl || ""]
      .map((u) => String(u).trim().replace(/\/+$/, "")).filter(Boolean))];
    if (!urls.length) return { ok: false, error: "API URL not set. Open Settings." };
    const headers = { "Content-Type": "application/json" };
    if (s.apiKey) headers["X-API-Key"] = s.apiKey;
    const body = JSON.stringify({ texts, options: { nltk: s.nltk, phrases: s.nltk } });
    const chars = texts.reduce((n, t) => n + t.length, 0);
    const extra = Math.min(60000, Math.ceil(chars / 10000) * 1000);
    for (const timeout of [30000 + extra, 70000 + extra]) {
      for (const url of urls) {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), timeout);
        try {
          const res = await fetch(url + "/v1/extract", {
            method: "POST", headers, body, signal: ctrl.signal,
          });
          if (res.status === 401) return { ok: false, error: "Invalid API key." };
          if (res.status === 413) return { ok: false, error: "Page chunk too large for the API." };
          if (res.ok) return { ok: true, data: await res.json() };
        } catch (e) { /* try the next server */ } finally { clearTimeout(timer); }
      }
    }
    return { ok: false, error: "Cannot reach any API server (check URLs, key, and that servers are awake)." };
  }

  // ---------- running ----------
  function batches(items) {
    const out = [];
    let cur = [], chars = 0;
    for (const it of items) {
      if (cur.length && (cur.length >= BATCH_ITEMS || chars + it.text.length > BATCH_CHARS)) {
        out.push(cur); cur = []; chars = 0;
      }
      cur.push(it); chars += it.text.length;
    }
    if (cur.length) out.push(cur);
    return out;
  }

  async function run(mode) {
    if (busy) return { ok: false, error: "Already running." };
    busy = true;
    try {
      let items;
      if (mode === "selection") {            // additive: existing highlights stay
        items = collectSelection();
        if (!items.length) {
          return { ok: false, error: "No text selected. Select some text on the page first." };
        }
      } else {                               // whole page: start from a clean slate
        clear();
        items = collectPage();
      }
      const before = countMarks().total;
      for (const batch of batches(items)) {
        const res = await callApi(batch.map((it) => it.text));
        if (!res?.ok) return { ok: false, error: res?.error || "No response." };
        res.data.results.forEach((spans, i) => wrap(batch[i], spans));
      }
      if (mode === "selection") window.getSelection()?.removeAllRanges();
      showLegend();
      const { total } = countMarks();
      return { ok: true, added: total - before, total, blocks: items.length };
    } catch (e) {
      return { ok: false, error: e.message };
    } finally {
      busy = false;
    }
  }

  const onMessage = (msg, _s, send) => {
    if (msg.cmd === "highlight" || msg.cmd === "selection") {
      run(msg.cmd).then(send);
      return true;
    }
    if (msg.cmd === "clear") { clear(); send({ ok: true }); }
  };
  ext.runtime.onMessage.addListener(onMessage);
  window.__nerHighlighter = {
    dispose: () => ext.runtime.onMessage.removeListener(onMessage),
  };
})();
