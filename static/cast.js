import { announce } from "./dom.js";

// Google Cast SDK setup -- fires once the CDN-loaded cast_sender.js script
// (see index.html) is ready. Guarded everywhere below by isCastAvailable(),
// since browsers with no Cast support (e.g. Firefox) never call this at all
// and castMedia()/the Cast button must degrade gracefully. Assigning to
// `window` here (rather than exporting a function the SDK could import) is
// required -- the SDK is a classic script, loaded with `async` specifically
// so it can arrive before or after this module and still find the callback,
// per Google's own integration pattern.
window.__onGCastApiAvailable = (isAvailable) => {
  if (!isAvailable) return;
  cast.framework.CastContext.getInstance().setOptions({
    receiverApplicationId: chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
    autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED,
  });
};

export function isCastAvailable() {
  return Boolean(window.cast && cast.framework);
}

// Sends `item` to whatever Cast device the user picks, via the browser's own
// Cast session UI (cast.framework handles device discovery/picking). The
// receiver has no cookie jar at all, so this mints a short-lived signed
// token (app/auth.py's create_cast_token) scoped to just this media item and
// appends it to an *absolute* URL -- Cast's receiver fetches the URL
// independently, not through this page's session/origin.
export async function castMedia(item, streamUrl, hlsPlaylistUrl) {
  if (!isCastAvailable()) return;

  // Chromecast's default media receiver doesn't support the Matroska
  // container at all, even though this app's own <video> player can direct-
  // play many .mkv files -- force the HLS path for casting regardless of
  // what the in-browser probe decided, rather than handing the receiver a
  // container it can never play.
  const isMkv = (item.path || "").toLowerCase().endsWith(".mkv");
  const castPlaylistUrl = isMkv ? `/api/stream/${item.id}/hls/playlist.m3u8` : hlsPlaylistUrl;
  const castPath = castPlaylistUrl || streamUrl;
  const contentType = castPlaylistUrl
    ? "application/x-mpegurl"
    : (item.media_type === "audio" ? "audio/mpeg" : "video/mp4");

  try {
    const res = await fetch(`/api/cast-token/${item.id}`, { method: "POST" });
    if (!res.ok) throw new Error("token mint failed");
    const { token } = await res.json();

    // location.origin is correct for the common case -- sender and TV
    // reaching this server via the same LAN address. Known, accepted
    // limitation: this breaks if the sender is browsing via localhost/
    // 127.0.0.1, which the TV could never reach; not solved here.
    const castUrl = new URL(castPath, location.origin);
    castUrl.searchParams.set("cast_token", token);

    const mediaInfo = new chrome.cast.media.MediaInfo(castUrl.toString(), contentType);
    mediaInfo.metadata = new chrome.cast.media.GenericMediaMetadata();
    mediaInfo.metadata.title = item.title || item.path;

    const request = new chrome.cast.media.LoadRequest(mediaInfo);
    const session = await cast.framework.CastContext.getInstance().requestSession();
    await session.loadMedia(request);
    announce(`Casting ${item.title || "media"}…`);
  } catch (err) {
    // Deliberately announce() rather than a player message -- that would
    // wipe #player-container, which would kill whatever's still playing
    // locally in the browser just because casting itself failed to start.
    announce("Couldn't start casting. Please try again.");
  }
}
