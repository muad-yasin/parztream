const listEl = document.getElementById("media-list");
const filterEl = document.getElementById("filter");
const scanBtn = document.getElementById("scan-btn");
const playerContainer = document.getElementById("player-container");

async function loadLibrary() {
  const type = filterEl.value;
  const url = type ? `/api/library?media_type=${type}` : "/api/library";
  const res = await fetch(url);
  const items = await res.json();
  renderList(items);
}

function renderList(items) {
  listEl.innerHTML = "";
  for (const item of items) {
    const li = document.createElement("li");
    const label = item.artist ? `${item.title} — ${item.artist}` : item.title;
    li.textContent = label || item.path;
    li.addEventListener("click", () => playMedia(item));
    listEl.appendChild(li);
  }
}

function playMedia(item) {
  playerContainer.innerHTML = "";
  const tag = item.media_type === "audio" ? "audio" : "video";
  const el = document.createElement(tag);
  el.controls = true;
  el.autoplay = true;
  el.src = `/api/stream/${item.id}`;
  if (tag === "video") {
    el.style.maxWidth = "100%";
  }
  playerContainer.appendChild(el);
}

filterEl.addEventListener("change", loadLibrary);

async function pollScanStatus() {
  while (true) {
    const res = await fetch("/api/scan/status");
    const status = await res.json();
    if (status.status !== "scanning") {
      return status;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

scanBtn.addEventListener("click", async () => {
  scanBtn.disabled = true;
  scanBtn.textContent = "Scanning...";
  const res = await fetch("/api/scan", { method: "POST" });
  if (res.status !== 409 && !res.ok) {
    scanBtn.disabled = false;
    scanBtn.textContent = "Scan library";
    return;
  }
  const status = await pollScanStatus();
  await loadLibrary();
  scanBtn.disabled = false;
  scanBtn.textContent = status.status === "error" ? "Scan failed — retry" : "Scan library";
});

loadLibrary();
