const dirInput = document.getElementById("dirInput");
const statusIcon = document.getElementById("statusIcon");
const runBtn = document.getElementById("runBtn");
const sendBtn = document.getElementById("sendBtn");
const modelSelect = document.getElementById("modelSelect");
const output = document.getElementById("output");
const downloads = document.getElementById("downloads");
const repoInfo = document.getElementById("repoInfo");
const promptPreview = document.getElementById("promptPreview");
const llmOutput = document.getElementById("llmOutput");

let debounceTimer = null;
let lastPromptPath = null;

function setIcon(state) {
  statusIcon.className = "icon " + state;
  statusIcon.title = state;
}

async function parseMaybeJson(res) {
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return await res.json();
  }
  const text = await res.text();
  return { ok: false, error: `HTTP ${res.status}`, raw: text };
}

async function checkDir(path) {
  setIcon("checking");
  try {
    const res = await fetch("/api/check_dir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path })
    });
    const data = await parseMaybeJson(res);
    if ((data.status || "") === "ok") {
      setIcon("ok");
      runBtn.disabled = false;
      repoInfo.textContent = data.repo_url ? `Repo: ${data.repo_url}` : "Repo: not detected";
    } else if ((data.status || "") === "missing") {
      setIcon("missing");
      runBtn.disabled = true;
      repoInfo.textContent = "";
    } else {
      setIcon("missing");
      runBtn.disabled = true;
      repoInfo.textContent = data.error ? `Error: ${data.error}` : "";
    }
  } catch {
    setIcon("missing");
    runBtn.disabled = true;
    repoInfo.textContent = "Network error.";
  }
}

dirInput.addEventListener("input", () => {
  clearTimeout(debounceTimer);
  setIcon("checking");
  runBtn.disabled = true;
  debounceTimer = setTimeout(() => checkDir(dirInput.value.trim()), 400);
});

function showDownloads(meta) {
  downloads.innerHTML = "";
  if (meta.log_file) {
    const name = meta.log_file.split("/").pop();
    const a = document.createElement("a");
    a.href = "/download/log/" + encodeURIComponent(name);
    a.textContent = "Download build log";
    downloads.appendChild(a);
  }
  if (meta.prompt_file) {
    const name = meta.prompt_file.split("/").pop();
    const a = document.createElement("a");
    a.href = "/download/prompt/" + encodeURIComponent(name);
    a.textContent = "Download ChatGPT prompt";
    downloads.appendChild(a);
  }
}

runBtn.addEventListener("click", async () => {
  output.textContent = "Running gradle build...";
  promptPreview.textContent = "";
  llmOutput.textContent = "";
  downloads.innerHTML = "";
  sendBtn.disabled = true;
  lastPromptPath = null;

  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: dirInput.value.trim() })
    });
    const data = await parseMaybeJson(res);
    if (!data.ok && data.result !== "failure" && data.result !== "success") {
      output.textContent = "Error: " + (data.error || "Unknown error");
      return;
    }
    if (data.result === "success") {
      output.textContent = "Build succeeded. The application will stop.";
      return;
    }
    // failure case
    output.textContent = "Build failed. Prompt prepared for ChatGPT.";
    showDownloads(data);
    if (data.prompt_text) {
      promptPreview.textContent = data.prompt_text;
    } else {
      // fallback fetch
      const r = await fetch("/api/get_last_prompt");
      const p = await parseMaybeJson(r);
      if (p.ok) promptPreview.textContent = p.prompt_text || "";
    }
    lastPromptPath = data.prompt_file || null;
    sendBtn.disabled = !lastPromptPath;
  } catch {
    output.textContent = "Request failed.";
  }
});

sendBtn.addEventListener("click", async () => {
  if (!lastPromptPath) return;
  llmOutput.textContent = "Sending to ChatGPT API...";
  try {
    const res = await fetch("/api/send_prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt_path: lastPromptPath,
        model: modelSelect.value
      })
    });
    const data = await parseMaybeJson(res);
    if (!data.ok) {
      llmOutput.textContent = "API error: " + (data.error || "Unknown error");
      return;
    }
    llmOutput.textContent = data.response_text || "(empty response)";
  } catch {
    llmOutput.textContent = "Network error.";
  }
});

// Initial state
setIcon("idle");
