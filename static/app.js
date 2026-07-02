const listEl = document.getElementById("media-list");
const filterEl = document.getElementById("filter");
const showSelectEl = document.getElementById("show-select");
const searchInputEl = document.getElementById("search-input");
const scanBtn = document.getElementById("scan-btn");
const logoutBtn = document.getElementById("logout-btn");
const playerContainer = document.getElementById("player-container");
const pagerEl = document.getElementById("pager");

const PAGE_SIZE = 50;
let offset = 0;

async function loadShowList() {
  const selected = showSelectEl.value;
  const res = await fetch("/api/shows");
  const shows = await res.json();

  showSelectEl.innerHTML = "";
  const allOption = document.createElement("option");
  allOption.value = "";
  allOption.textContent = "All shows";
  showSelectEl.appendChild(allOption);

  for (const show of shows) {
    const option = document.createElement("option");
    option.value = show.show_name;
    option.textContent = `${show.show_name} (${show.episode_count})`;
    showSelectEl.appendChild(option);
  }
  showSelectEl.value = selected;
}

async function loadLibrary() {
  const type = filterEl.value;
  const showName = showSelectEl.value;
  const query = searchInputEl.value.trim();
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset });
  if (type) params.set("media_type", type);
  if (showName) params.set("show_name", showName);
  if (query) params.set("q", query);

  const res = await fetch(`/api/library?${params}`);
  const data = await res.json();
  renderList(data.items, query);
  renderPager(data.total);
}

function renderList(items, query) {
  listEl.innerHTML = "";

  if (items.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty-message";
    empty.textContent = query ? `No results for "${query}"` : "No media yet — try scanning the library.";
    listEl.appendChild(empty);
    return;
  }

  for (const item of items) {
    const li = document.createElement("li");

    const img = document.createElement("img");
    img.className = "thumb";
    img.loading = "lazy";
    img.src = `/api/library/${item.id}/art`;
    img.onerror = () => { img.style.visibility = "hidden"; };
    li.appendChild(img);

    const label = document.createElement("span");
    const episodeTag = item.show_name ? `S${item.season_number}E${item.episode_number} — ` : "";
    const title = item.artist ? `${item.title} — ${item.artist}` : (item.title || item.path);
    label.textContent = episodeTag + title;
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

async function playMedia(item) {
  const streamUrl = `/api/stream/${item.id}`;

  playerContainer.innerHTML = "";
  const preparing = document.createElement("p");
  preparing.className = "player-message";
  preparing.textContent = "Preparing…";
  playerContainer.appendChild(preparing);

  // A tiny ranged probe first: if the file needs a container/audio fix
  // (see app/transcode.py), this is also what triggers and waits for that
  // one-time conversion, so by the time we hand the URL to <video>/<audio>
  // it's already cached and plays instantly. It also lets us show a clear
  // message instead of a silent player failure for codecs we can't fix.
  let probe;
  try {
    probe = await fetch(streamUrl, { headers: { Range: "bytes=0-1" } });
  } catch (err) {
    preparing.textContent = "Couldn't reach the server.";
    return;
  }

  playerContainer.innerHTML = "";

  if (probe.status === 415) {
    const msg = document.createElement("p");
    msg.className = "player-message";
    msg.textContent = "This file's video format can't be played in the browser. ";
    const link = document.createElement("a");
    // ?original=1 bypasses the same compatibility check that just 415'd --
    // without it this link would 415 too, since it hits the same endpoint.
    link.href = `${streamUrl}?original=1`;
    link.textContent = "Download it instead";
    link.download = "";
    msg.appendChild(link);
    playerContainer.appendChild(msg);
    return;
  }

  const tag = item.media_type === "audio" ? "audio" : "video";
  const el = document.createElement(tag);
  el.controls = true;
  el.autoplay = true;
  el.src = streamUrl;
  if (tag === "video") {
    el.style.maxWidth = "100%";
    // A missing sidecar subtitle file just 404s -- the browser ignores
    // that silently, no need to check first like the codec probe above.
    const track = document.createElement("track");
    track.kind = "subtitles";
    track.src = `/api/library/${item.id}/subtitles`;
    track.default = true;
    track.label = "Subtitles";
    el.appendChild(track);
  }
  playerContainer.appendChild(el);
}

filterEl.addEventListener("change", () => {
  offset = 0;
  loadLibrary();
});

showSelectEl.addEventListener("change", () => {
  offset = 0;
  loadLibrary();
});

let searchDebounceTimer = null;
searchInputEl.addEventListener("input", () => {
  clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => {
    offset = 0;
    loadLibrary();
  }, 300);
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
  await loadShowList();
  await loadLibrary();
  scanBtn.disabled = false;
  scanBtn.textContent = status.status === "error" ? "Scan failed — retry" : "Scan library";
});

logoutBtn.addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  window.location.href = "/login.html";
});

async function init() {
  const res = await fetch("/api/setup/status");
  const status = await res.json();
  if (!status.configured) {
    window.location.href = "/setup.html";
    return;
  }
  await loadShowList();
  await loadLibrary();
}

init();
