/* Recording a label from the page you are looking at.
 *
 * The rationale box appears the moment confidence drops below medium, rather
 * than being a field somebody may or may not fill in. The store refuses a weak
 * claim without one — surfacing that here means the refusal arrives while the
 * reasoning is still in your head, which is the only time it gets written down.
 */

const $ = (id) => document.getElementById(id);
const status = $("status");

async function config() {
  const saved = await chrome.storage.local.get(["endpoint", "token"]);
  return { endpoint: saved.endpoint || "http://127.0.0.1:8787", token: saved.token || "" };
}

(async () => {
  const { endpoint, token } = await config();
  try {
    const response = await fetch(`${endpoint}/health`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const data = await response.json();
    for (const c of data.categories || []) {
      $("category").append(new Option(c, c, c === "service", c === "service"));
    }
    for (const c of data.confidences || []) {
      $("confidence").append(new Option(c, c, c === "medium", c === "medium"));
    }
    if (!data.writable) {
      status.textContent = "Server is read-only — start it with --writable.";
      $("save").disabled = true;
    }
  } catch {
    status.textContent = "No server. Start chainscope-serve, then set the token in options.";
    $("save").disabled = true;
  }
})();

$("confidence").addEventListener("change", (e) => {
  const weak = ["low", "speculative"].includes(e.target.value);
  $("why-row").style.display = weak ? "block" : "none";
});

$("save").addEventListener("click", async () => {
  const { endpoint, token } = await config();
  status.style.color = "#6b7280";
  status.textContent = "Recording…";
  try {
    const response = await fetch(`${endpoint}/tag`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        address: $("address").value.trim(),
        label: $("label").value.trim(),
        category: $("category").value,
        confidence: $("confidence").value,
        rationale: $("why").value.trim(),
      }),
    });
    const data = await response.json();
    if (response.ok) {
      status.style.color = "#2e7d32";
      status.textContent = `Recorded — ${data.recorded.label} (${data.recorded.confidence})`;
    } else {
      status.style.color = "#c62828";
      status.textContent = data.error || `Refused (${response.status})`;
    }
  } catch (err) {
    status.style.color = "#c62828";
    status.textContent = String(err);
  }
});
