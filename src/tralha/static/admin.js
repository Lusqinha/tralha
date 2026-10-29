// Panel enhancements. Every action also works without JS, as a plain form.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

function dismissFlashToast() {
  const toast = $(".toast");
  if (!toast) return;
  const close = () => { toast.classList.add("leaving"); setTimeout(() => toast.remove(), 200); };
  $("button", toast)?.addEventListener("click", close);
  if (!toast.classList.contains("error")) setTimeout(close, 4000);
  const url = new URL(location.href);
  url.searchParams.delete("msg");
  history.replaceState(null, "", url);
}

function confirmDestructiveForms() {
  $$("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => { if (!confirm(form.dataset.confirm)) e.preventDefault(); });
  });
}

function setupCopyButtons() {
  $$("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(btn.dataset.copy); } catch { return; }
      const label = $("span", btn) || btn;
      const original = label.textContent;
      btn.classList.add("done");
      label.textContent = "Copied";
      setTimeout(() => { btn.classList.remove("done"); label.textContent = original; }, 1600);
    });
  });
  $$("input[readonly]").forEach((el) => el.addEventListener("focus", () => el.select()));
}

function rememberGridOrListView() {
  const views = $$("[data-view]");
  if (!views.length) return;
  const apply = (view) => {
    views.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
    $$("[data-view-panel]").forEach((p) => { p.hidden = p.dataset.viewPanel !== view; });
    store.set("tralha:view", view);
  };
  views.forEach((b) => b.addEventListener("click", () => apply(b.dataset.view)));
  apply(store.get("tralha:view") || "grid");
}

function filterCurrentFolderByName() {
  const filter = $("#filter");
  if (!filter) return;
  filter.addEventListener("input", () => {
    const q = filter.value.trim().toLowerCase();
    $$("[data-name]").forEach((el) => { el.hidden = q && !el.dataset.name.toLowerCase().includes(q); });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); filter.focus(); }
  });
}

function openNewFolder() {
  const folderToggle = $("#new-folder-toggle"), folderForm = $("#new-folder");
  if (!folderToggle || !folderForm) return;
  folderToggle.addEventListener("click", () => {
    folderForm.hidden = !folderForm.hidden;
    folderToggle.hidden = !folderForm.hidden;
    if (!folderForm.hidden) $("input", folderForm).focus();
  });
  folderForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("input", folderForm).value.trim().replace(/^\/+|\/+$/g, "");
    if (!name) return;
    const url = new URL(folderForm.dataset.base, location.origin);
    url.searchParams.set("prefix", (folderForm.dataset.prefix || "") + name + "/");
    location.href = url;
  });
  folderForm.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { folderForm.hidden = true; folderToggle.hidden = false; }
  });
}

function setupUploadWithProgressAndDrop() {
  const upload = $("#upload");
  if (!upload) return;
  const input = $("input[type=file]", upload);
  const drop = $("#drop"), progress = $("#progress");

  const send = (files) => {
    if (!files.length) return;
    const data = new FormData(upload);
    data.delete("files");
    [...files].forEach((f) => data.append("files", f, f.name));
    const total = [...files].reduce((n, f) => n + f.size, 0);
    $("#progress-label").textContent = files.length === 1 ? files[0].name : `${files.length} files`;
    progress.hidden = false;

    const xhr = new XMLHttpRequest();
    xhr.open("POST", upload.action);
    xhr.upload.addEventListener("progress", (e) => {
      const pct = Math.round((e.loaded / (e.lengthComputable ? e.total : total)) * 100);
      $("#progress-fill").style.width = pct + "%";
      $("#progress-pct").textContent = pct < 100 ? pct + "%" : "processing…";
    });
    xhr.addEventListener("load", () => { location.href = xhr.responseURL || location.href; });
    xhr.addEventListener("error", () => { progress.hidden = true; alert("Upload failed. Check your connection and try again."); });
    xhr.send(data);
  };

  input.addEventListener("change", () => send(input.files));
  $$("label[for=files]").forEach((pickerLabel) => openPickerFromKeyboard(pickerLabel, input));


  let depth = 0;
  const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  window.addEventListener("dragenter", (e) => { if (hasFiles(e)) { depth++; drop.classList.add("on"); } });
  window.addEventListener("dragleave", (e) => { if (hasFiles(e) && --depth <= 0) { depth = 0; drop.classList.remove("on"); } });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; drop.classList.remove("on");
    send(e.dataTransfer.files);
  });
}

function openPickerFromKeyboard(label, input) {
  label.tabIndex = 0;
  label.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  });
}

function switchMarkdownRenderedOrSource() {
  $$("[data-tab]").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$("[data-tab]").forEach((t) => t.setAttribute("aria-pressed", String(t === tab)));
      $$("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== tab.dataset.tab; });
    });
  });
}

function showQualityOnlyForLossy() {
  const quality = $("#quality");
  if (!quality) return;
  const out = $("#quality-out"), range = $("input", quality);
  range.addEventListener("input", () => { out.value = range.value; });
  const sync = () => { quality.hidden = $("input[name=compression]:checked")?.value !== "lossy"; };
  $$("input[name=compression]").forEach((r) => r.addEventListener("change", sync));
  sync();
}

dismissFlashToast();
confirmDestructiveForms();
setupCopyButtons();
rememberGridOrListView();
filterCurrentFolderByName();
openNewFolder();
setupUploadWithProgressAndDrop();
switchMarkdownRenderedOrSource();
showQualityOnlyForLossy();
