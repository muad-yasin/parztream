import { announce } from "./dom.js";
import { playerState } from "./state.js";
import { castMedia, isCastAvailable } from "./cast.js";
import { saveResumePosition, getResumePosition, clearResumePosition } from "./resume.js";

const playerContainer = document.getElementById("player-container");

// "pointer: coarse" identifies touch-primary input specifically, not just
// a narrow screen -- more reliable than a width-based check, since a small
// desktop browser window shouldn't trigger phone-style fullscreen-on-play,
// and a large-screen tablet in touch mode should.
const isTouchDevice = window.matchMedia("(pointer: coarse)").matches;

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

// Lazily injects hls.min.js (532KB) only the first time a file actually
// needs HLS -- most playback is direct-play and never touches this at all,
// so there's no reason to pay that download/parse cost on every page view.
// Cached as a promise (not just a boolean) so concurrent callers -- e.g. a
// user clicking a second HLS file before the first load finishes -- share
// the one in-flight <script> load instead of injecting it twice.
let hlsLoadPromise = null;

function ensureHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (!hlsLoadPromise) {
    hlsLoadPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/hls.min.js";
      script.onload = () => resolve(window.Hls);
      script.onerror = () => reject(new Error("hls.min.js failed to load"));
      document.head.appendChild(script);
    });
  }
  return hlsLoadPromise;
}

// Fully stops and tears down the current player: pauses/releases the media
// element, destroys any active hls.js instance (otherwise its internal
// segment-fetch loop keeps running against a detached element), clears the
// row highlight, and empties the container back to its initial state.
export function stopPlayer() {
  const el = playerContainer.querySelector("video, audio");
  if (el) {
    el.pause();
    el.removeAttribute("src");
    el.load();
  }
  if (playerState.activeHls) {
    playerState.activeHls.destroy();
    playerState.activeHls = null;
  }
  if (playerState.activeRowBtn) {
    playerState.activeRowBtn.classList.remove("row-btn-active");
    playerState.activeRowBtn.removeAttribute("aria-current");
    playerState.activeRowBtn = null;
  }
  playerState.activePlayingId = null;
  playerContainer.innerHTML = "";
  announce("Playback stopped.");
}

export async function playMedia(item, rowBtn) {
  const streamUrl = `/api/stream/${item.id}`;

  if (playerState.activeRowBtn) {
    playerState.activeRowBtn.classList.remove("row-btn-active");
    playerState.activeRowBtn.removeAttribute("aria-current");
  }
  // A previous player's hls.js instance (if any) is about to be replaced by
  // the innerHTML wipe below, which would otherwise leave it running its
  // segment-fetch loop against a now-detached <video> -- same cleanup
  // stopPlayer() does when the user closes the player explicitly.
  if (playerState.activeHls) {
    playerState.activeHls.destroy();
    playerState.activeHls = null;
  }
  playerState.activePlayingId = item.id;
  if (rowBtn) {
    rowBtn.classList.add("row-btn-active");
    rowBtn.setAttribute("aria-current", "true");
  }
  playerState.activeRowBtn = rowBtn || null;

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

  if (isCastAvailable()) {
    const castBtn = document.createElement("button");
    castBtn.type = "button";
    castBtn.className = "dismiss-btn dismiss-btn-overlay cast-btn";
    castBtn.setAttribute("aria-label", "Cast to device");
    castBtn.textContent = "Cast";
    castBtn.addEventListener("click", () => castMedia(item, streamUrl, hlsPlaylistUrl));
    playerContainer.appendChild(castBtn);
  }

  if (hlsPlaylistUrl) {
    // hls.js first, native canPlayType() second -- the order matters and
    // is the one hls.js's own docs prescribe. Chromium 149 answers "maybe"
    // to canPlayType("application/vnd.apple.mpegurl") while being unable
    // to actually demux HLS (caught by tests/e2e against a real browser:
    // checking native support first sent Chromium down the native path and
    // every HLS playback died with a decode error). Safari still works via
    // either branch: macOS has MSE so hls.js runs, and iOS -- where hls.js
    // can't run at all -- falls through to its genuinely-native support.
    // ensureHls() only fetches hls.min.js the first time this branch is
    // ever reached; a failed load (offline, blocked) resolves to no Hls,
    // same as if the script had never been present, and falls through to
    // the native branch below exactly as before.
    const Hls = await ensureHls().catch(() => null);
    if (Hls && Hls.isSupported()) {
      playerState.activeHls = new Hls();
      playerState.activeHls.on(Hls.Events.ERROR, (event, data) => {
        if (!data.fatal) return;
        playerState.activeHls.destroy();
        playerState.activeHls = null;
        showPlaybackError(item, "Playback failed and couldn't recover.");
      });
      playerState.activeHls.loadSource(hlsPlaylistUrl);
      playerState.activeHls.attachMedia(el);
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
