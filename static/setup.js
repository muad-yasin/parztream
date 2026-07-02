const upBtn = document.getElementById("up-btn");
const currentPathEl = document.getElementById("current-path");
const folderListEl = document.getElementById("folder-list");
const addFolderBtn = document.getElementById("add-folder-btn");
const selectedListEl = document.getElementById("selected-list");
const noFoldersMessage = document.getElementById("no-folders-message");
const saveBtn = document.getElementById("save-btn");
const errorEl = document.getElementById("setup-error");

let currentPath = null;
let currentParent = null;
const selectedFolders = [];

async function browse(path) {
  const params = path ? new URLSearchParams({ path }) : new URLSearchParams();
  const res = await fetch(`/api/setup/browse?${params}`);
  if (!res.ok) {
    errorEl.textContent = "Couldn't open that folder.";
    return;
  }
  const data = await res.json();
  currentPath = data.path;
  currentParent = data.parent;
  errorEl.textContent = "";
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
    li.textContent = name;
    li.addEventListener("click", () => {
      const separator = currentPath.endsWith("/") || currentPath.endsWith("\\") ? "" : "/";
      browse(`${currentPath}${separator}${name}`);
    });
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
  if (currentPath && !selectedFolders.includes(currentPath)) {
    selectedFolders.push(currentPath);
    renderSelected();
  }
});

saveBtn.addEventListener("click", async () => {
  saveBtn.disabled = true;
  errorEl.textContent = "";
  const res = await fetch("/api/setup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ media_dirs: selectedFolders }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    errorEl.textContent = body.detail || "Something went wrong saving your folders.";
    saveBtn.disabled = false;
    return;
  }
  window.location.href = "/";
});

browse();
