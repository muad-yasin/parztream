// Playback position is persisted client-side only (no backend support for
// this yet) -- good enough for "resume where I left off on this device",
// which is the case that actually comes up for a home-LAN single-user tool.
function resumeKey(id) {
  return `parztream:resume:${id}`;
}

export function saveResumePosition(id, time) {
  try {
    localStorage.setItem(resumeKey(id), String(time));
  } catch (err) {
    // localStorage can be unavailable (private browsing, quota) -- resume
    // is a nice-to-have, never worth breaking playback over.
  }
}

export function clearResumePosition(id) {
  try {
    localStorage.removeItem(resumeKey(id));
  } catch (err) {
    // See above.
  }
}

export function getResumePosition(id) {
  try {
    const value = localStorage.getItem(resumeKey(id));
    return value ? parseFloat(value) : 0;
  } catch (err) {
    return 0;
  }
}
