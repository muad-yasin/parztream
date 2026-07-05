import { announce, showMessage } from "./dom.js";
import { playerState } from "./state.js";
import { createMediaRow, createPosterTile, renderPager, markActiveIfCurrent } from "./rows.js";
import { playMedia } from "./player.js";

const listEl = document.getElementById("media-list");
const filterEl = document.getElementById("filter");
const searchInputEl = document.getElementById("search-input");
const pagerEl = document.getElementById("pager");

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

export async function loadLibrary() {
  const type = filterEl.value;
  const query = searchInputEl.value.trim();
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset });
  if (type) params.set("media_type", type);
  if (query) params.set("q", query);

  showMessage(listEl, "Loading…");

  let res;
  try {
    res = await fetch(`/api/library?${params}`);
  } catch (err) {
    showMessage(listEl, "Couldn't reach the server. Try again.");
    announce("Couldn't reach the server.");
    return;
  }
  if (!res.ok) {
    showMessage(listEl, "Couldn't load the library. Try again.");
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

function renderList(items, query) {
  listEl.innerHTML = "";
  playerState.activeRowBtn = null;

  if (items.length === 0) {
    showMessage(listEl, query ? `No results for "${query}"` : "No media yet — try scanning the library.");
    return;
  }

  for (const item of items) {
    listEl.appendChild(createMediaRow(item));
  }
}

async function loadMoviesGrid() {
  const params = new URLSearchParams({
    is_movie: "true", media_type: "video", limit: MOVIES_PAGE_SIZE, offset: moviesOffset,
  });

  showMessage(moviesGridEl, "Loading movies…", { tag: "p" });

  let res;
  try {
    res = await fetch(`/api/library?${params}`);
  } catch (err) {
    // Previously a silent `return` -- the grid was left however it was
    // (blank on first load), indistinguishable from "no movies yet".
    showMessage(moviesGridEl, "Couldn't reach the server. Try again.", { tag: "p" });
    announce("Couldn't load movies.");
    return;
  }
  if (!res.ok) {
    showMessage(moviesGridEl, "Couldn't load movies. Try again.", { tag: "p" });
    announce("Couldn't load movies.");
    return;
  }

  const data = await res.json();
  moviesGridEl.innerHTML = "";
  if (data.items.length === 0) {
    showMessage(moviesGridEl, "No movies yet — try scanning the library.", { tag: "p" });
  } else {
    for (const item of data.items) {
      const tile = createPosterTile({
        mediaId: item.id,
        label: item.title || item.path,
        onClick: (tileEl) => playMedia(item, tileEl),
      });
      markActiveIfCurrent(tile, item.id);
      moviesGridEl.appendChild(tile);
    }
  }

  renderPager(moviesPagerEl, data.total, moviesOffset, MOVIES_PAGE_SIZE, (newOffset) => {
    moviesOffset = newOffset;
    loadMoviesGrid();
  });
}

async function loadShowsGrid() {
  showMessage(showsGridEl, "Loading TV shows…", { tag: "p" });

  let shows;
  try {
    const res = await fetch("/api/shows");
    if (!res.ok) throw new Error("shows-request-failed");
    shows = await res.json();
  } catch (err) {
    showMessage(showsGridEl, "Couldn't load TV shows. Try again.", { tag: "p" });
    announce("Couldn't load TV shows.");
    return;
  }

  showsGridEl.innerHTML = "";
  if (shows.length === 0) {
    showMessage(showsGridEl, "No TV shows yet — try scanning the library.", { tag: "p" });
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

// Guards against a slower, superseded request (e.g. clicking from one show
// to another quickly) overwriting the currently-requested show's content
// with stale data once it finally resolves.
let showViewRequestId = 0;

async function loadShowView(showName) {
  const requestId = ++showViewRequestId;

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
    if (requestId !== showViewRequestId) return;
    announce("Couldn't reach the server.");
    return;
  }
  if (requestId !== showViewRequestId) return;
  if (!episodesRes.ok || !extrasRes.ok) {
    announce(`Couldn't load ${showName}.`);
    return;
  }

  const episodes = (await episodesRes.json()).items;
  const extras = (await extrasRes.json()).items;
  if (requestId !== showViewRequestId) return;

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

export function setActiveView(view) {
  homeViewEl.hidden = view !== "home";
  searchViewEl.hidden = view !== "search";
  showViewEl.hidden = view !== "show";
}

export async function render() {
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
