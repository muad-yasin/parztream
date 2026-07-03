const listEl = document.getElementById("media-list");
const filterEl = document.getElementById("filter");
const showSelectEl = document.getElementById("show-select");
const searchInputEl = document.getElementById("search-input");
const scanBtn = document.getElementById("scan-btn");
const logoutBtn = document.getElementById("logout-btn");
const playerContainer = document.getElementById("player-container");
const pagerEl = document.getElementById("pager");
const announcerEl = document.getElementById("status-announcer");
const scanBanner = document.getElementById("scan-banner");
const scanBannerText = document.getElementById("scan-banner-text");

const PAGE_SIZE = 50;
let offset = 0;

// "pointer: coarse" identifies touch-primary input specifically, not just
// a narrow screen -- more reliable than a width-based check, since a small
// desktop browser window shouldn't trigger phone-style fullscreen-on-play,
// and a large-screen tablet in touch mode should.
const isTouchDevice = window.matchMedia("(pointer: coarse)").matches;

function announce(message) {
  announcerEl.textContent = message;
}

function showScanBanner(text) {
  scanBannerText.textContent = text;
  scanBanner.hidden = false;
}

function hideScanBanner() {
  scanBanner.hidden = true;
}

// Replaces the whole list with a single centered message -- used for the
// loading state, network/server errors, and (via renderList) empty results,
// so there's always some feedback in the list area instead of a blank gap.
function showListMessage(text) {
  listEl.innerHTML = "";
  const li = document.createElement("li");
  li.className = "empty-message";
  li.textContent = text;
  listEl.appendChild(li);
}

function requestVideoFullscreen(el) {
  if (!isTouchDevice) return;
  try {
    if (el.requestFullscreen) {
      // Fullscreen isn't guaranteed to succeed (browser policy, permissions
      // policy, etc.) -- never let a rejection surface as an error, playback
      // should carry on regardless either way.
      el.requestFullscreen().catch(() => {});
    } else if (el.webkitEnterFullscreen) {
      // iOS Safari doesn't implement the standard Fullscreen API for
      // <video> -- it has its own proprietary method that opens the native
      // fullscreen video player instead.
      el.webkitEnterFullscreen();
    }
  } catch (err) {
    // Same reasoning: never let this break playback.
  }
}

async function loadShowList() {
  const selected = showSelectEl.value;
  let shows;
  try {
    const res = await fetch("/api/shows");
    if (!res.ok) throw new Error("shows-request-failed");
    shows = await res.json();
  } catch (err) {
    // Non-fatal -- the show filter just stays at "All shows" until the
    // next successful load (e.g. after the next scan).
    return;
  }

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

  showListMessage("Loading…");

  let res;
  try {
    res = await fetch(`/api/library?${params}`);
  } catch (err) {
    showListMessage("Couldn't reach the server. Try again.");
    announce("Couldn't reach the server.");
    return;
  }
  if (!res.ok) {
    showListMessage("Couldn't load the library. Try again.");
    announce("Couldn't load the library.");
    return;
  }

  const data = await res.json();
  renderList(data.items, query);
  renderPager(data.total);

  if (query) {
    announce(data.total === 0 ? `No results for "${query}"` : `${data.total} result${data.total === 1 ? "" : "s"} for "${query}"`);
  }
}

// Tracks which item is currently loaded in the player so its row can be
// highlighted -- kept as an id (survives across re-renders from filtering/
// paging) plus the live button reference (for restyling without a
// full reload when play starts).
let activePlayingId = null;
let activeRowBtn = null;

function renderList(items, query) {
  listEl.innerHTML = "";
  activeRowBtn = null;

  if (items.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty-message";
    empty.textContent = query ? `No results for "${query}"` : "No media yet — try scanning the library.";
    listEl.appendChild(empty);
    return;
  }

  for (const item of items) {
    const li = document.createElement("li");

    const rowBtn = document.createElement("button");
    rowBtn.type = "button";
    rowBtn.className = "row-btn";

    const img = document.createElement("img");
    img.className = "thumb";
    img.loading = "lazy";
    img.src = `/api/library/${item.id}/art`;
    img.alt = ""; // decorative -- the label span below already names the item
    img.onerror = () => {
      // Previously just hid the broken image, leaving an empty gap that
      // looked like a rendering bug rather than "no artwork available".
      const placeholder = document.createElement("span");
      placeholder.className = "thumb thumb-placeholder";
      placeholder.setAttribute("aria-hidden", "true");
      placeholder.textContent = item.media_type === "audio" ? "♪" : "▶";
      img.replaceWith(placeholder);
    };
    rowBtn.appendChild(img);

    const label = document.createElement("span");
    label.className = "row-label";
    const episodeTag = item.show_name ? `S${item.season_number}E${item.episode_number} — ` : "";
    const title = item.artist ? `${item.title} — ${item.artist}` : (item.title || item.path);
    label.textContent = episodeTag + title;
    label.title = label.textContent; // full text on hover once ellipsis truncates it
    rowBtn.appendChild(label);

    if (item.id === activePlayingId) {
      rowBtn.classList.add("row-btn-active");
      rowBtn.setAttribute("aria-current", "true");
      activeRowBtn = rowBtn;
    }

    rowBtn.addEventListener("click", () => playMedia(item, rowBtn));
    li.appendChild(rowBtn);
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

// Playback position is persisted client-side only (no backend support for
// this yet) -- good enough for "resume where I left off on this device",
// which is the case that actually comes up for a home-LAN single-user tool.
function resumeKey(id) {
  return `parztream:resume:${id}`;
}

function saveResumePosition(id, time) {
  try {
    localStorage.setItem(resumeKey(id), String(time));
  } catch (err) {
    // localStorage can be unavailable (private browsing, quota) -- resume
    // is a nice-to-have, never worth breaking playback over.
  }
}

function clearResumePosition(id) {
  try {
    localStorage.removeItem(resumeKey(id));
  } catch (err) {
    // See above.
  }
}

function getResumePosition(id) {
  try {
    const value = localStorage.getItem(resumeKey(id));
    return value ? parseFloat(value) : 0;
  } catch (err) {
    return 0;
  }
}

async function playMedia(item, rowBtn) {
  const streamUrl = `/api/stream/${item.id}`;

  if (activeRowBtn) {
    activeRowBtn.classList.remove("row-btn-active");
    activeRowBtn.removeAttribute("aria-current");
  }
  activePlayingId = item.id;
  if (rowBtn) {
    rowBtn.classList.add("row-btn-active");
    rowBtn.setAttribute("aria-current", "true");
  }
  activeRowBtn = rowBtn || null;

  playerContainer.innerHTML = "";
  const preparing = document.createElement("p");
  preparing.className = "player-message";
  preparing.textContent = "Preparing…";
  playerContainer.appendChild(preparing);
  // The player sits at the top of <main>, but a click far down a long list
  // can still leave it above the viewport -- scroll it into view up front
  // so "Preparing…"/errors/the eventual player are all actually visible.
  playerContainer.scrollIntoView({ behavior: "smooth", block: "start" });
  announce(`Preparing ${item.title || "playback"}…`);

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
    announce("Couldn't reach the server.");
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
    announce("This file's video format can't be played in the browser. A download link is available.");
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
  el.addEventListener("loadedmetadata", () => {
    const resumeAt = getResumePosition(item.id);
    // Ignore a saved position in the last few seconds -- that's effectively
    // "finished", resuming there would just replay the very end.
    if (resumeAt > 5 && resumeAt < el.duration - 5) {
      el.currentTime = resumeAt;
    }
  });
  el.addEventListener("timeupdate", () => saveResumePosition(item.id, el.currentTime));
  el.addEventListener("ended", () => clearResumePosition(item.id));

  playerContainer.appendChild(el);
  if (tag === "video") {
    requestVideoFullscreen(el);
  }
  announce(`Now playing ${item.title || tag}.`);
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
    let res;
    try {
      res = await fetch("/api/scan/status");
    } catch (err) {
      throw new Error("scan-status-unreachable");
    }
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
  showScanBanner("Scanning your library for media — this can take a few minutes for large libraries.");
  announce("Scanning library…");
  try {
    const res = await fetch("/api/scan", { method: "POST" });
    if (res.status !== 409 && !res.ok) {
      throw new Error("scan-failed-to-start");
    }
    const status = await pollScanStatus();
    offset = 0;
    await loadShowList();
    await loadLibrary();
    scanBtn.textContent = status.status === "error" ? "Scan failed — retry" : "Scan library";
    announce(status.status === "error" ? "Scan failed." : "Scan complete.");
  } catch (err) {
    scanBtn.textContent = "Scan failed — retry";
    announce("Couldn't reach the server. Scan failed to start.");
  } finally {
    scanBtn.disabled = false;
    hideScanBanner();
  }
});

logoutBtn.addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  window.location.href = "/login.html";
});

async function init() {
  let status;
  try {
    const res = await fetch("/api/setup/status");
    status = await res.json();
  } catch (err) {
    showListMessage("Couldn't reach the server. Try reloading the page.");
    return;
  }
  if (!status.configured) {
    window.location.href = "/setup.html";
    return;
  }

  // Setup triggers a background scan right after saving folders and
  // redirects straight here -- without this check, a first-time user lands
  // on what looks like an empty, broken library with no indication a scan
  // is already running.
  try {
    const scanRes = await fetch("/api/scan/status");
    const scanStatus = await scanRes.json();
    if (scanStatus.status === "scanning") {
      showScanBanner("Setting up your library — scanning for media now. This can take a few minutes.");
      announce("Scanning library…");
      await pollScanStatus();
      hideScanBanner();
    }
  } catch (err) {
    // Non-fatal -- fall through and load whatever's already in the library.
  }

  await loadShowList();
  await loadLibrary();
}

init();
