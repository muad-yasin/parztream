import { announce, showMessage } from "./dom.js";
import { setActiveView, render } from "./views.js";
import { showScanBanner, hideScanBanner, renderScanDiagnostics, pollScanStatus } from "./scan.js";

const logoutBtn = document.getElementById("logout-btn");
const listEl = document.getElementById("media-list");

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
    showMessage(listEl, "Couldn't reach the server. Try reloading the page.");
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
    showMessage(listEl, "Couldn't reach the server. Try reloading the page.");
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
