const announcerEl = document.getElementById("status-announcer");

export function announce(message) {
  announcerEl.textContent = message;
}

// Replaces a container's whole content with a single centered message --
// used for loading states, network/server errors, and empty results across
// the flat list and both home-view grids, so there's always some feedback
// in place of a blank area instead of silence. `tag` matches whatever the
// container's other children use ("li" inside a <ul>, "p" inside a grid
// <div>) so the message doesn't break the container's own markup rules.
export function showMessage(container, text, { tag = "li" } = {}) {
  container.innerHTML = "";
  const el = document.createElement(tag);
  el.className = "empty-message";
  el.textContent = text;
  container.appendChild(el);
}
