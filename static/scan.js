import { announce } from "./dom.js";
import { render } from "./views.js";

const scanBtn = document.getElementById("scan-btn");
const scanBanner = document.getElementById("scan-banner");
const scanBannerText = document.getElementById("scan-banner-text");
const scanDiagnosticsEl = document.getElementById("scan-diagnostics");
const scanDiagnosticsSummaryEl = document.getElementById("scan-diagnostics-summary");
const scanDiagnosticsListEl = document.getElementById("scan-diagnostics-list");
const scanDiagnosticsCloseBtn = document.getElementById("scan-diagnostics-close");

export function showScanBanner(text) {
  scanBannerText.textContent = text;
  scanBanner.hidden = false;
}

export function hideScanBanner() {
  scanBanner.hidden = true;
}

// Renders a post-scan summary of per-file problems -- stays hidden entirely
// when nothing went wrong, so the common all-clean case looks exactly as
// clean as before this existed.
export function renderScanDiagnostics(status) {
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

export async function pollScanStatus() {
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
