const upBtn = document.getElementById("up-btn");
const currentPathEl = document.getElementById("current-path");
const folderListEl = document.getElementById("folder-list");
const addFolderBtn = document.getElementById("add-folder-btn");
const selectedListEl = document.getElementById("selected-list");
const noFoldersMessage = document.getElementById("no-folders-message");
const saveBtn = document.getElementById("save-btn");
const errorEl = document.getElementById("setup-error");
const addStatusEl = document.getElementById("add-status");

let currentPath = null;
let currentParent = null;
const selectedFolders = [];

// Guards against a slower, superseded request overwriting the browser view
// with stale directory listings -- e.g. clicking two different subfolders
// in quick succession, where the first click's response could otherwise
// arrive after the second and render the wrong folder's contents.
let browseRequestId = 0;

async function browse(path) {
  const requestId = ++browseRequestId;
  const params = path ? new URLSearchParams({ path }) : new URLSearchParams();
  let res;
  try {
    res = await fetch(`/api/setup/browse?${params}`);
  } catch (err) {
    if (requestId !== browseRequestId) return;
    errorEl.textContent = "Couldn't reach the server. Check your connection and try again.";
    return;
  }
  if (requestId !== browseRequestId) return;
  if (!res.ok) {
    errorEl.textContent = "Couldn't open that folder.";
    return;
  }
  const data = await res.json();
  if (requestId !== browseRequestId) return;
  currentPath = data.path;
  currentParent = data.parent;
  errorEl.textContent = "";
  addStatusEl.textContent = "";
  renderBrowser(data.directories);
}

function renderBrowser(directories) {
  currentPathEl.textContent = currentPath;
  upBtn.disabled = !currentParent;

  folderListEl.innerHTML = "";
  if (directories.length === 0) {
    const empty = document.createElement("li");
    empty.className = "setup-hint";
    empty.textContent = "No subfolders here.";
    folderListEl.appendChild(empty);
    return;
  }
  for (const name of directories) {
    const li = document.createElement("li");
    const rowBtn = document.createElement("button");
    rowBtn.type = "button";
    rowBtn.className = "row-btn";
    rowBtn.textContent = name;
    rowBtn.addEventListener("click", () => {
      const separator = currentPath.endsWith("/") || currentPath.endsWith("\\") ? "" : "/";
      browse(`${currentPath}${separator}${name}`);
    });
    li.appendChild(rowBtn);
    folderListEl.appendChild(li);
  }
}

function renderSelected() {
  selectedListEl.innerHTML = "";
  noFoldersMessage.style.display = selectedFolders.length === 0 ? "block" : "none";
  saveBtn.disabled = selectedFolders.length === 0;

  for (const folder of selectedFolders) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = folder;
    li.appendChild(label);

    const removeBtn = document.createElement("button");
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", () => {
      const index = selectedFolders.indexOf(folder);
      if (index !== -1) selectedFolders.splice(index, 1);
      renderSelected();
    });
    li.appendChild(removeBtn);

    selectedListEl.appendChild(li);
  }
}

upBtn.addEventListener("click", () => {
  if (currentParent) browse(currentParent);
});

addFolderBtn.addEventListener("click", () => {
  if (!currentPath) return;
  if (selectedFolders.includes(currentPath)) {
    // Was previously a silent no-op -- a user clicking "Add this folder"
    // twice had no way to tell whether the second click registered.
    addStatusEl.textContent = "That folder is already added.";
    return;
  }
  selectedFolders.push(currentPath);
  renderSelected();
  addStatusEl.textContent = `Added "${currentPath}".`;
});

saveBtn.addEventListener("click", async () => {
  saveBtn.disabled = true;
  errorEl.textContent = "";
  let res;
  try {
    res = await fetch("/api/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ media_dirs: selectedFolders }),
    });
  } catch (err) {
    errorEl.textContent = "Couldn't reach the server. Check your connection and try again.";
    saveBtn.disabled = false;
    return;
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    errorEl.textContent = body.detail || "Something went wrong saving your folders.";
    saveBtn.disabled = false;
    return;
  }
  window.location.href = "/";
});

browse();
