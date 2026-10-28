// ============================================================
// ui.js — utilidades de UI compartidas entre páginas del panel
// ============================================================

let toastTimer = null;

export function showToast(text, type = "success") {
  const toastEl = document.getElementById("toast");
  if (!toastEl) return;
  clearTimeout(toastTimer);
  document.getElementById("toast-text").textContent = text;
  toastEl.className = `toast${type === "error" ? " toast--error" : ""}`;
  toastEl.hidden = false;
  toastTimer = setTimeout(() => {
    toastEl.hidden = true;
  }, 2600);
}

export function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}
