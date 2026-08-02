const endpoint = document.getElementById("endpoint");
const token = document.getElementById("token");
const status = document.getElementById("status");

chrome.storage.local.get(["endpoint", "token"]).then((saved) => {
  endpoint.value = saved.endpoint || "http://127.0.0.1:8787";
  token.value = saved.token || "";
});

document.getElementById("save").addEventListener("click", async () => {
  const values = { endpoint: endpoint.value.trim(), token: token.value.trim() };
  await chrome.storage.local.set(values);
  status.textContent = "Saved. Checking…";
  try {
    const response = await fetch(`${values.endpoint}/health`, {
      headers: { Authorization: `Bearer ${values.token}` },
    });
    const data = await response.json();
    status.textContent = response.ok
      ? `Connected — ${data.store}${data.writable ? "" : " (read-only)"}`
      : `Refused: ${data.error || response.status}`;
    status.style.color = response.ok ? "#2e7d32" : "#c62828";
  } catch {
    status.textContent = "No server on that endpoint. Is chainscope-serve running?";
    status.style.color = "#c62828";
  }
});
