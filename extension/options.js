const ext = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);
const msg = (t, err) => { $("msg").textContent = t; $("msg").className = err ? "err" : ""; };

ext.storage.local.get({ apiUrls: [], apiUrl: "", apiKey: "", nltk: true }).then((s) => {
  const urls = [...(s.apiUrls || [])];
  if (s.apiUrl && !urls.includes(s.apiUrl)) urls.push(s.apiUrl);
  $("urls").value = urls.join("\n");
  $("key").value = s.apiKey;
  $("nltk").checked = s.nltk;
});

function parseUrls() {
  const out = [];
  for (const raw of $("urls").value.split(/[\s,]+/).filter(Boolean)) {
    let u;
    try { u = new URL(raw); } catch { throw new Error("Enter valid URLs, one per line."); }
    const local = ["localhost", "127.0.0.1"].includes(u.hostname);
    if (u.protocol !== "https:" && !local) throw new Error(`Use https:// for ${u.host} (http is only allowed for localhost).`);
    out.push(u.origin);
  }
  return [...new Set(out)];
}

$("save").addEventListener("click", async () => {
  let urls;
  try { urls = parseUrls(); } catch (e) { return msg(e.message, true); }
  if (!urls.length) return msg("Enter at least one server URL.", true);

  // PERMISSION_BLOCK_START
  // Must run inside the click gesture, before other awaits.
  const granted = await ext.permissions.request({ origins: urls.map((u) => u + "/*") });
  if (!granted) return msg("Permission to contact those servers was denied.", true);
  // PERMISSION_BLOCK_END

  await ext.storage.local.set({
    apiUrls: urls, apiUrl: "", apiKey: $("key").value.trim(), nltk: $("nltk").checked,
  });
  msg(`Saved ${urls.length} server${urls.length === 1 ? "" : "s"}.`);
});

$("test").addEventListener("click", async () => {
  msg("Testing… (a sleeping free-tier server can take up to a minute)");
  const res = await ext.runtime.sendMessage({ type: "health" });
  if (!res?.ok) return msg(res?.error || "Failed.", true);
  msg(res.data.map((r) => r.ok ? `OK    ${r.url} (${r.ms} ms)` : `FAIL  ${r.url}: ${r.error}`).join("\n"));
});
