const ext = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);
const status = (msg, err) => { $("status").textContent = msg; $("status").className = err ? "err" : ""; };
$("ver").textContent = "v" + ext.runtime.getManifest().version;

async function activeTab() {
  const [tab] = await ext.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function ensureInjected(tab) {
  await ext.scripting.insertCSS({ target: { tabId: tab.id }, files: ["content.css"] });
  await ext.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
}

async function pageCommand(cmd, button, startMsg) {
  button.disabled = true;
  status(startMsg);
  try {
    const tab = await activeTab();
    await ensureInjected(tab);
    const res = await ext.tabs.sendMessage(tab.id, { cmd });
    if (!res?.ok) return status(res?.error || "Failed.", true);
    if (cmd === "clear") return status("Cleared.");
    const verb = cmd === "selection" ? "Added" : "Highlighted";
    status(`${verb} ${res.added} entities (${res.total} on this page).`);
  } catch (e) {
    status("Cannot run here: " + (e.message || e) +
           " (browser pages are blocked; try reloading the page).", true);
  } finally {
    button.disabled = false;
  }
}

$("go").addEventListener("click", () =>
  pageCommand("highlight", $("go"), "Analyzing… (the first call may take a minute if the server was asleep)"));
$("sel").addEventListener("click", () =>
  pageCommand("selection", $("sel"), "Analyzing selection…"));
$("clear").addEventListener("click", () => pageCommand("clear", $("clear"), ""));
$("settings").addEventListener("click", () => ext.runtime.openOptionsPage());

// Analyze pasted text inside the popup (works even where selection is lost).
function render(text, spans) {
  const out = $("out");
  out.textContent = "";
  let pos = 0, n = 0;
  for (const s of [...spans].sort((a, b) => a.start - b.start)) {
    if (s.start < pos) continue;
    out.append(text.slice(pos, s.start));
    const mark = document.createElement("mark");
    mark.className = `ner-hl ner-${s.label}`;
    mark.title = s.label;
    mark.textContent = text.slice(s.start, s.end);
    out.append(mark);
    pos = s.end;
    n++;
  }
  out.append(text.slice(pos));
  return n;
}

$("analyze").addEventListener("click", async () => {
  const text = $("paste").value;
  if (!text.trim()) return status("Paste some text first.", true);
  $("analyze").disabled = true;
  status("Analyzing…");
  try {
    const res = await ext.runtime.sendMessage({ type: "extract", texts: [text] });
    if (!res?.ok) return status(res?.error || "Failed.", true);
    status(`Found ${render(text, res.data.results[0])} entities.`);
  } catch (e) {
    status("Failed: " + (e.message || e), true);
  } finally {
    $("analyze").disabled = false;
  }
});
