const listEl = document.getElementById("media-list");
const filterEl = document.getElementById("filter");
const searchInputEl = document.getElementById("search-input");
const scanBtn = document.getElementById("scan-btn");
const logoutBtn = document.getElementById("logout-btn");
const playerContainer = document.getElementById("player-container");
const pagerEl = document.getElementById("pager");
const announcerEl = document.getElementById("status-announcer");
const scanBanner = document.getElementById("scan-banner");
const scanBannerText = document.getElementById("scan-banner-text");
const scanDiagnosticsEl = document.getElementById("scan-diagnostics");
const scanDiagnosticsSummaryEl = document.getElementById("scan-diagnostics-summary");
const scanDiagnosticsListEl = document.getElementById("scan-diagnostics-list");
const scanDiagnosticsCloseBtn = document.getElementById("scan-diagnostics-close");

const homeViewEl = document.getElementById("home-view");
const searchViewEl = document.getElementById("search-view");
const showViewEl = document.getElementById("show-view");
const moviesGridEl = document.getElementById("movies-grid");
const moviesPagerEl = document.getElementById("movies-pager");
const showsGridEl = document.getElementById("shows-grid");
const showBackBtn = document.getElementById("show-back-btn");
const showViewTitleEl = document.getElementById("show-view-title");
const showSeasonsEl = document.getElementById("show-seasons");
const showExtrasEl = document.getElementById("show-extras");
const showExtrasCountEl = document.getElementById("show-extras-count");
const showExtrasListEl = document.getElementById("show-extras-list");

const PAGE_SIZE = 50;
const MOVIES_PAGE_SIZE = 50;
let offset = 0;
let moviesOffset = 0;

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

// Renders a post-scan summary of per-file problems -- stays hidden entirely
// when nothing went wrong, so the common all-clean case looks exactly as
// clean as before this existed.
function renderScanDiagnostics(status) {
  const failedCount = status.failed_count || 0;
  const incompleteCount = status.incomplete_count || 0;
  if (!failedCount && !incompleteCount) {
    scanDiagnosticsEl.hidden = true;
    return;
  }

  const parts = [`${status.scanned_count || 0} files scanned`];
  if (failedCount) parts.push(`${failedCount} failed`);
  if (incompleteCount) parts.push(`${incompleteCount} with incomplete metadata`);
  scanDiagnosticsSummaryEl.textContent = parts.join(", ");

  scanDiagnosticsListEl.innerHTML = "";
  for (const { path, error } of status.failed_examples || []) {
    const li = document.createElement("li");
    li.textContent = `Failed: ${path} — ${error}`;
    scanDiagnosticsListEl.appendChild(li);
  }
  for (const { path } of status.incomplete_examples || []) {
    const li = document.createElement("li");
    li.textContent = `Incomplete metadata: ${path}`;
    scanDiagnosticsListEl.appendChild(li);
  }

  scanDiagnosticsEl.hidden = false;
}

scanDiagnosticsCloseBtn.addEventListener("click", (event) => {
  // The button lives inside <summary> (the only child a closed <details>
  // renders at all) -- without preventDefault/stopPropagation, activating
  // it would also toggle the disclosure open/closed, same click.
  event.preventDefault();
  event.stopPropagation();
  scanDiagnosticsEl.hidden = true;
  announce("Scan summary dismissed.");
});

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

// Renders a dismissible status/error message into #player-container,
// replacing whatever's currently there. downloadUrl, when given, appends a
// "Download it instead" link (callers suppress this for audio items).
function showPlayerMessage(text, { downloadUrl } = {}) {
  playerContainer.innerHTML = "";
  const msg = document.createElement("div");
  msg.className = "player-message";
  const textSpan = document.createElement("span");
  textSpan.textContent = text;
  msg.appendChild(textSpan);
  if (downloadUrl) {
    textSpan.textContent += " ";
    const link = document.createElement("a");
    link.href = downloadUrl;
    link.textContent = "Download it instead";
    link.download = "";
    msg.appendChild(link);
  }
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "dismiss-btn";
  closeBtn.setAttribute("aria-label", "Dismiss message");
  closeBtn.textContent = "×";
  closeBtn.addEventListener("click", () => {
    playerContainer.innerHTML = "";
    announce("Message dismissed.");
  });
  msg.appendChild(closeBtn);
  playerContainer.appendChild(msg);
  announce(text);
}

// Replaces the player with a clear error message -- used by both the
// native media-element `error` event and hls.js's fatal-error event, so a
// playback failure that happens *after* the pre-play codec probe (a
// corrupt file, a network blip mid-segment, overlapping-job corruption)
// is never just a silently frozen/black player. A "download instead" link
// is offered the same way the pre-play 415 case already does, since the
// underlying file may still be playable in another app even if this
// browser choked on it mid-stream.
function showPlaybackError(item, message) {
  showPlayerMessage(message, {
    downloadUrl: item.media_type !== "audio" ? `/api/stream/${item.id}?original=1` : undefined,
  });
}

const MEDIA_ERROR_MESSAGES = {
  1: "Playback was aborted.",
  2: "A network error interrupted playback.",
  3: "This file's content couldn't be decoded.",
  4: "This file's format isn't supported by this browser.",
};

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

async function loadLibrary() {
  const type = filterEl.value;
  const query = searchInputEl.value.trim();
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset });
  if (type) params.set("media_type", type);
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
  renderPager(pagerEl, data.total, offset, PAGE_SIZE, (newOffset) => {
    offset = newOffset;
    loadLibrary();
  });

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
// The active hls.js instance, if the current player is HLS-backed -- tracked
// so it can be torn down (stopPlayer / a new playMedia call) instead of left
// running its segment-fetch loop against a detached <video> element.
let activeHls = null;

// Builds the shared "thumbnail + label" row used by the flat search list,
// a show's per-season episode lists, and its Extras list -- one visual/
// interaction pattern (real <button>, decorative thumbnail, active-row
// highlighting) reused everywhere instead of three near-duplicates.
function createMediaRow(item) {
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
  const episodeTag = item.show_name && item.season_number != null
    ? `S${item.season_number}E${item.episode_number} — `
    : "";
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
  return li;
}

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
    listEl.appendChild(createMediaRow(item));
  }
}

// Generic prev/info/next pager, shared by the flat search list and the
// Movies grid -- each keeps its own offset/page-size/reload closure rather
// than this function knowing about either view.
function renderPager(container, total, currentOffset, pageSize, onPageChange) {
  container.innerHTML = "";
  if (total === 0) return;

  const prevBtn = document.createElement("button");
  prevBtn.textContent = "Prev";
  prevBtn.disabled = currentOffset === 0;
  prevBtn.addEventListener("click", () => onPageChange(Math.max(0, currentOffset - pageSize)));

  const info = document.createElement("span");
  const start = currentOffset + 1;
  const end = Math.min(currentOffset + pageSize, total);
  info.textContent = `${start}-${end} of ${total}`;

  const nextBtn = document.createElement("button");
  nextBtn.textContent = "Next";
  nextBtn.disabled = currentOffset + pageSize >= total;
  nextBtn.addEventListener("click", () => onPageChange(currentOffset + pageSize));

  container.append(prevBtn, info, nextBtn);
}

// Builds the shared poster-tile button used by both the Movies and TV
// Shows grids -- a taller, vertical sibling of createMediaRow's row-btn,
// same decorative-thumbnail/active-state conventions.
function createPosterTile({ mediaId, label, onClick, isAudio }) {
  const tile = document.createElement("button");
  tile.type = "button";
  tile.className = "poster-tile";

  const img = document.createElement("img");
  img.className = "poster-thumb";
  img.loading = "lazy";
  img.src = `/api/library/${mediaId}/art`;
  img.alt = ""; // decorative -- the label below already names the item
  img.onerror = () => {
    const placeholder = document.createElement("span");
    placeholder.className = "poster-thumb poster-thumb-placeholder";
    placeholder.setAttribute("aria-hidden", "true");
    placeholder.textContent = isAudio ? "♪" : "▶";
    img.replaceWith(placeholder);
  };
  tile.appendChild(img);

  const labelEl = document.createElement("span");
  labelEl.className = "poster-label";
  labelEl.textContent = label;
  labelEl.title = label;
  tile.appendChild(labelEl);

  tile.addEventListener("click", () => onClick(tile));
  return tile;
}

async function loadMoviesGrid() {
  const params = new URLSearchParams({
    is_movie: "true", media_type: "video", limit: MOVIES_PAGE_SIZE, offset: moviesOffset,
  });

  let res;
  try {
    res = await fetch(`/api/library?${params}`);
  } catch (err) {
    return;
  }
  if (!res.ok) return;

  const data = await res.json();
  moviesGridEl.innerHTML = "";
  if (data.items.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-message";
    empty.textContent = "No movies yet — try scanning the library.";
    moviesGridEl.appendChild(empty);
  } else {
    for (const item of data.items) {
      const tile = createPosterTile({
        mediaId: item.id,
        label: item.title || item.path,
        onClick: (tileEl) => playMedia(item, tileEl),
      });
      if (item.id === activePlayingId) {
        tile.classList.add("row-btn-active");
        tile.setAttribute("aria-current", "true");
        activeRowBtn = tile;
      }
      moviesGridEl.appendChild(tile);
    }
  }

  renderPager(moviesPagerEl, data.total, moviesOffset, MOVIES_PAGE_SIZE, (newOffset) => {
    moviesOffset = newOffset;
    loadMoviesGrid();
  });
}

async function loadShowsGrid() {
  let shows;
  try {
    const res = await fetch("/api/shows");
    if (!res.ok) throw new Error("shows-request-failed");
    shows = await res.json();
  } catch (err) {
    return;
  }

  showsGridEl.innerHTML = "";
  if (shows.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-message";
    empty.textContent = "No TV shows yet — try scanning the library.";
    showsGridEl.appendChild(empty);
    return;
  }

  for (const show of shows) {
    const tile = createPosterTile({
      mediaId: show.sample_media_id,
      label: `${show.show_name} (${show.episode_count})`,
      onClick: () => {
        location.hash = `#/show/${encodeURIComponent(show.show_name)}`;
      },
    });
    showsGridEl.appendChild(tile);
  }
}

async function loadShowView(showName) {
  showViewTitleEl.textContent = showName;
  showSeasonsEl.innerHTML = "";
  showExtrasListEl.innerHTML = "";
  showExtrasEl.hidden = true;
  announce(`Loading ${showName}…`);

  const episodesParams = new URLSearchParams({ show_name: showName, limit: 500 });
  const extrasParams = new URLSearchParams({ show_name: showName, extras: "true", limit: 500 });

  let episodesRes, extrasRes;
  try {
    [episodesRes, extrasRes] = await Promise.all([
      fetch(`/api/library?${episodesParams}`),
      fetch(`/api/library?${extrasParams}`),
    ]);
  } catch (err) {
    announce("Couldn't reach the server.");
    return;
  }
  if (!episodesRes.ok || !extrasRes.ok) {
    announce(`Couldn't load ${showName}.`);
    return;
  }

  const episodes = (await episodesRes.json()).items;
  const extras = (await extrasRes.json()).items;

  // Episodes already arrive ordered by season_number, episode_number --
  // grouping preserves that order, it doesn't need to re-sort.
  let currentSeason = null;
  let currentSeasonList = null;
  for (const item of episodes) {
    if (item.season_number !== currentSeason) {
      currentSeason = item.season_number;
      const heading = document.createElement("h3");
      heading.textContent = currentSeason === 0 ? "Specials" : `Season ${currentSeason}`;
      showSeasonsEl.appendChild(heading);
      currentSeasonList = document.createElement("ul");
      showSeasonsEl.appendChild(currentSeasonList);
    }
    currentSeasonList.appendChild(createMediaRow(item));
  }
  if (episodes.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-message";
    empty.textContent = "No episodes found for this show.";
    showSeasonsEl.appendChild(empty);
  }

  if (extras.length > 0) {
    showExtrasCountEl.textContent = extras.length;
    for (const item of extras) {
      showExtrasListEl.appendChild(createMediaRow(item));
    }
    showExtrasEl.hidden = false;
  }

  announce(`${showName}: ${episodes.length} episode${episodes.length === 1 ? "" : "s"}${extras.length ? `, ${extras.length} extra${extras.length === 1 ? "" : "s"}` : ""}.`);
}

// Decides which of the three views (home grids / flat search list / a
// show's detail page) is currently active, based on the URL hash and the
// existing type/search controls -- a non-empty search or the Audio filter
// always falls back to the flat list rather than trying to force those
// results into the grid metaphor, and #/show/<name> takes over the whole
// content area regardless of the other controls.
function currentRoute() {
  const hash = location.hash;
  if (hash.startsWith("#/show/")) {
    try {
      return { view: "show", showName: decodeURIComponent(hash.slice("#/show/".length)) };
    } catch (err) {
      // Malformed percent-encoding (e.g. a truncated or hand-edited hash)
      // -- fall back to the home view instead of throwing and leaving the
      // whole content area blank.
      return { view: "home" };
    }
  }
  if (filterEl.value === "audio" || searchInputEl.value.trim() !== "") {
    return { view: "search" };
  }
  return { view: "home" };
}

function setActiveView(view) {
  homeViewEl.hidden = view !== "home";
  searchViewEl.hidden = view !== "search";
  showViewEl.hidden = view !== "show";
}

async function render() {
  const route = currentRoute();
  setActiveView(route.view);

  if (route.view === "show") {
    await loadShowView(route.showName);
  } else if (route.view === "search") {
    offset = 0;
    await loadLibrary();
  } else {
    moviesOffset = 0;
    await Promise.all([loadMoviesGrid(), loadShowsGrid()]);
  }
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

// Fully stops and tears down the current player: pauses/releases the media
// element, destroys any active hls.js instance (otherwise its internal
// segment-fetch loop keeps running against a detached element), clears the
// row highlight, and empties the container back to its initial state.
function stopPlayer() {
  const el = playerContainer.querySelector("video, audio");
  if (el) {
    el.pause();
    el.removeAttribute("src");
    el.load();
  }
  if (activeHls) {
    activeHls.destroy();
    activeHls = null;
  }
  if (activeRowBtn) {
    activeRowBtn.classList.remove("row-btn-active");
    activeRowBtn.removeAttribute("aria-current");
    activeRowBtn = null;
  }
  activePlayingId = null;
  playerContainer.innerHTML = "";
  announce("Playback stopped.");
}

async function playMedia(item, rowBtn) {
  const streamUrl = `/api/stream/${item.id}`;

  if (activeRowBtn) {
    activeRowBtn.classList.remove("row-btn-active");
    activeRowBtn.removeAttribute("aria-current");
  }
  // A previous player's hls.js instance (if any) is about to be replaced by
  // the innerHTML wipe below, which would otherwise leave it running its
  // segment-fetch loop against a now-detached <video> -- same cleanup
  // stopPlayer() does when the user closes the player explicitly.
  if (activeHls) {
    activeHls.destroy();
    activeHls = null;
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

  // A tiny ranged probe first: this just asks the server whether the file
  // plays directly or needs HLS (see app/transcode.py) -- it no longer
  // waits for any conversion itself, that now happens lazily as the
  // playlist/segments are actually requested below. It also lets us show a
  // clear message instead of a silent player failure for codecs we can't
  // fix at all.
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 20000);
  let probe;
  try {
    probe = await fetch(streamUrl, { headers: { Range: "bytes=0-1" }, signal: controller.signal });
  } catch (err) {
    preparing.textContent = err.name === "AbortError"
      ? "This is taking longer than expected to prepare. Try again in a moment."
      : "Couldn't reach the server.";
    announce(preparing.textContent);
    return;
  } finally {
    clearTimeout(timeoutId);
  }

  playerContainer.innerHTML = "";

  if (probe.status === 415) {
    // ?original=1 bypasses the same compatibility check that just 415'd --
    // without it this link would 415 too, since it hits the same endpoint.
    showPlayerMessage("This file's video format can't be played in the browser. ", {
      downloadUrl: `${streamUrl}?original=1`,
    });
    return;
  }

  if (!probe.ok) {
    showPlayerMessage("Couldn't prepare this file for playback. Please try again.");
    return;
  }

  // A JSON body means the file needs HLS (container/audio remux) rather
  // than being directly streamable -- anything else (200/206 on the probe)
  // means it can be played directly, same as before.
  let hlsPlaylistUrl = null;
  if ((probe.headers.get("content-type") || "").includes("application/json")) {
    const body = await probe.json().catch(() => null);
    hlsPlaylistUrl = body && body.hls_playlist;
    if (!hlsPlaylistUrl) {
      showPlayerMessage("Couldn't prepare this file for playback. Please try again.");
      return;
    }
  }

  const tag = item.media_type === "audio" ? "audio" : "video";
  const el = document.createElement(tag);
  el.controls = true;
  el.autoplay = true;
  if (!hlsPlaylistUrl) {
    el.src = streamUrl;
  }
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
  // Covers direct-play files, Safari's native HLS path (no hls.js
  // involved there), and decode errors surfaced by hls.js itself onto the
  // element -- one listener for every "something went wrong mid-playback"
  // case that isn't already caught by the pre-play probe above.
  el.addEventListener("error", () => {
    const code = el.error && el.error.code;
    showPlaybackError(item, MEDIA_ERROR_MESSAGES[code] || "Playback failed.");
  });

  playerContainer.appendChild(el);

  // Overlaid on the media element itself (#player-container's existing
  // `position: sticky` already establishes the containing block for this)
  // rather than a separate button below it, so it reads as part of the
  // player rather than a generic page control.
  const closePlayerBtn = document.createElement("button");
  closePlayerBtn.type = "button";
  closePlayerBtn.className = "dismiss-btn dismiss-btn-overlay";
  closePlayerBtn.setAttribute("aria-label", "Close player");
  closePlayerBtn.textContent = "×";
  closePlayerBtn.addEventListener("click", () => stopPlayer());
  playerContainer.appendChild(closePlayerBtn);

  if (hlsPlaylistUrl) {
    // hls.js first, native canPlayType() second -- the order matters and
    // is the one hls.js's own docs prescribe. Chromium 149 answers "maybe"
    // to canPlayType("application/vnd.apple.mpegurl") while being unable
    // to actually demux HLS (caught by tests/e2e against a real browser:
    // checking native support first sent Chromium down the native path and
    // every HLS playback died with a decode error). Safari still works via
    // either branch: macOS has MSE so hls.js runs, and iOS -- where hls.js
    // can't run at all -- falls through to its genuinely-native support.
    if (window.Hls && window.Hls.isSupported()) {
      activeHls = new Hls();
      activeHls.on(Hls.Events.ERROR, (event, data) => {
        if (!data.fatal) return;
        activeHls.destroy();
        activeHls = null;
        showPlaybackError(item, "Playback failed and couldn't recover.");
      });
      activeHls.loadSource(hlsPlaylistUrl);
      activeHls.attachMedia(el);
    } else if (el.canPlayType("application/vnd.apple.mpegurl")) {
      // iOS Safari: no MSE, so hls.js can't run -- but HLS support is
      // native in <video> there.
      el.src = hlsPlaylistUrl;
    } else {
      showPlayerMessage("This browser can't play this file's format.");
      return;
    }
  }

  if (tag === "video") {
    requestVideoFullscreen(el);
  }
  announce(`Now playing ${item.title || tag}.`);
}

filterEl.addEventListener("change", () => {
  // Switching to/from "Audio" (or back to "All"/"Video") can move us
  // between the flat search list and the home grids -- go through the
  // hash-aware router rather than assuming we're already on the list.
  if (location.hash.startsWith("#/show/")) location.hash = "#/";
  else render();
});

let searchDebounceTimer = null;
searchInputEl.addEventListener("input", () => {
  clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => {
    if (location.hash.startsWith("#/show/")) location.hash = "#/";
    else render();
  }, 300);
});

showBackBtn.addEventListener("click", () => {
  location.hash = "#/";
});

window.addEventListener("hashchange", render);

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
  scanDiagnosticsEl.hidden = true; // clear any stale summary from a previous scan
  announce("Scanning library…");
  try {
    const res = await fetch("/api/scan", { method: "POST" });
    if (res.status !== 409 && !res.ok) {
      throw new Error("scan-failed-to-start");
    }
    const status = await pollScanStatus();
    // A scan can add/remove shows or movies out from under whichever view
    // is currently on screen (including a show page whose episodes/extras
    // just changed) -- re-run the same routing logic used on load/hash
    // change rather than assuming the flat list is what's visible.
    await render();
    scanBtn.textContent = status.status === "error" ? "Scan failed — retry" : "Scan library";
    renderScanDiagnostics(status);
    if (status.status === "error") {
      announce("Scan failed.");
    } else if (status.failed_count || status.incomplete_count) {
      announce(`Scan complete with ${status.failed_count || 0} failed and ${status.incomplete_count || 0} with incomplete metadata.`);
    } else {
      announce("Scan complete.");
    }
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
  let res;
  try {
    res = await fetch("/api/setup/status");
  } catch (err) {
    // The home grids are visible by default -- force the flat-list view
    // on so this message is actually seen instead of landing in a hidden
    // container behind the (still-empty) grids.
    setActiveView("search");
    showListMessage("Couldn't reach the server. Try reloading the page.");
    return;
  }
  if (res.status === 401) {
    // An expired/invalid session -- the JSON body here is just
    // {"detail": "Not authenticated"}, with no "configured" field at all.
    // Without this check, `!status.configured` below is trivially true
    // for that shape regardless of whether the library is actually
    // configured, sending an already-set-up user to /setup.html instead
    // of back to the login page.
    window.location.href = `/login.html?next=${encodeURIComponent(location.pathname + location.search + location.hash)}`;
    return;
  }
  if (!res.ok) {
    setActiveView("search");
    showListMessage("Couldn't reach the server. Try reloading the page.");
    return;
  }
  const status = await res.json();
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
      const finalStatus = await pollScanStatus();
      hideScanBanner();
      renderScanDiagnostics(finalStatus);
    }
  } catch (err) {
    // Non-fatal -- fall through and load whatever's already in the library.
  }

  await render();
}

init();
