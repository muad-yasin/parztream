import { playerState } from "./state.js";
import { playMedia } from "./player.js";

// Shared "broken thumbnail -> icon placeholder" fallback for both the flat
// row thumbnails and the poster-grid tiles -- previously duplicated nearly
// verbatim in both createMediaRow and createPosterTile; now there are two
// real call sites, which is exactly when the skill says extracting is
// justified rather than premature.
function attachThumbnailFallback(img, { isAudio, thumbClass, placeholderClass }) {
  img.onerror = () => {
    // Previously just hid the broken image, leaving an empty gap that
    // looked like a rendering bug rather than "no artwork available".
    const placeholder = document.createElement("span");
    placeholder.className = `${thumbClass} ${placeholderClass}`;
    placeholder.setAttribute("aria-hidden", "true");
    placeholder.textContent = isAudio ? "♪" : "▶";
    img.replaceWith(placeholder);
  };
}

// Marks `el` as the currently-playing row/tile if `id` matches, and records
// it in playerState.activeRowBtn so stopPlayer()/playMedia() can clear the
// highlight later -- shared by createMediaRow (which does this itself) and
// views.js's movies-grid rendering (which builds tiles via createPosterTile,
// a plain builder with no playback-state awareness of its own).
export function markActiveIfCurrent(el, id) {
  if (id !== playerState.activePlayingId) return;
  el.classList.add("row-btn-active");
  el.setAttribute("aria-current", "true");
  playerState.activeRowBtn = el;
}

// Builds the shared "thumbnail + label" row used by the flat search list,
// a show's per-season episode lists, and its Extras list -- one visual/
// interaction pattern (real <button>, decorative thumbnail, active-row
// highlighting) reused everywhere instead of three near-duplicates.
export function createMediaRow(item) {
  const li = document.createElement("li");

  const rowBtn = document.createElement("button");
  rowBtn.type = "button";
  rowBtn.className = "row-btn";

  const img = document.createElement("img");
  img.className = "thumb";
  img.loading = "lazy";
  img.src = `/api/library/${item.id}/art`;
  img.alt = ""; // decorative -- the label span below already names the item
  attachThumbnailFallback(img, {
    isAudio: item.media_type === "audio",
    thumbClass: "thumb",
    placeholderClass: "thumb-placeholder",
  });
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

  markActiveIfCurrent(rowBtn, item.id);

  rowBtn.addEventListener("click", () => playMedia(item, rowBtn));
  li.appendChild(rowBtn);
  return li;
}

// Generic prev/info/next pager, shared by the flat search list and the
// Movies grid -- each keeps its own offset/page-size/reload closure rather
// than this function knowing about either view.
export function renderPager(container, total, currentOffset, pageSize, onPageChange) {
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
// same decorative-thumbnail convention. Unlike createMediaRow, it doesn't
// mark itself active -- callers building a movie tile pass the item's id
// through markActiveIfCurrent themselves (show tiles are never "active",
// there's no player state to reflect there).
export function createPosterTile({ mediaId, label, onClick, isAudio }) {
  const tile = document.createElement("button");
  tile.type = "button";
  tile.className = "poster-tile";

  const img = document.createElement("img");
  img.className = "poster-thumb";
  img.loading = "lazy";
  img.src = `/api/library/${mediaId}/art`;
  img.alt = ""; // decorative -- the label below already names the item
  attachThumbnailFallback(img, {
    isAudio,
    thumbClass: "poster-thumb",
    placeholderClass: "poster-thumb-placeholder",
  });
  tile.appendChild(img);

  const labelEl = document.createElement("span");
  labelEl.className = "poster-label";
  labelEl.textContent = label;
  labelEl.title = label;
  tile.appendChild(labelEl);

  tile.addEventListener("click", () => onClick(tile));
  return tile;
}
