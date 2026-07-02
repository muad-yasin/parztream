const listEl = document.getElementById("media-list");
const filterEl = document.getElementById("filter");
const scanBtn = document.getElementById("scan-btn");
const playerContainer = document.getElementById("player-container");
const pagerEl = document.getElementById("pager");

const PAGE_SIZE = 50;
let offset = 0;

async function loadLibrary() {
  const type = filterEl.value;
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset });
  if (type) params.set("media_type", type);

  const res = await fetch(`/api/library?${params}`);
  const data = await res.json();
  renderList(data.items);
  renderPager(data.total);
}

function renderList(items) {
  listEl.innerHTML = "";
  for (const item of items) {
    const li = document.createElement("li");

    const img = document.createElement("img");
    img.className = "thumb";
    img.loading = "lazy";
    img.src = `/api/library/${item.id}/art`;
    img.onerror = () => { img.style.visibility = "hidden"; };
    li.appendChild(img);

    const label = document.createElement("span");
    label.textContent = item.artist ? `${item.title} — ${item.artist}` : (item.title || item.path);
    li.appendChild(label);

    li.addEventListener("click", () => playMedia(item));
    listEl.appendChild(li);
  }
}

function renderPager(total) {
  pagerEl.innerHTML = "";
  if (total === 0) return;

  const prevBtn = document.createElement("button");
  prevBtn.textContent = "Prev";
  prevBtn.disabled = offset === 0;
  prevBtn.addEventListener("click", () => {
    offset = Math.max(0, offset - PAGE_SIZE);
    loadLibrary();
  });

  const info = document.createElement("span");
  const start = offset + 1;
  const end = Math.min(offset + PAGE_SIZE, total);
  info.textContent = `${start}-${end} of ${total}`;

  const nextBtn = document.createElement("button");
  nextBtn.textContent = "Next";
  nextBtn.disabled = offset + PAGE_SIZE >= total;
  nextBtn.addEventListener("click", () => {
    offset += PAGE_SIZE;
    loadLibrary();
  });

  pagerEl.append(prevBtn, info, nextBtn);
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

filterEl.addEventListener("change", () => {
  offset = 0;
  loadLibrary();
});

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
  offset = 0;
  await loadLibrary();
  scanBtn.disabled = false;
  scanBtn.textContent = status.status === "error" ? "Scan failed — retry" : "Scan library";
});

loadLibrary();
