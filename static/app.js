/* 局域网文件互传 — 前端逻辑 */
"use strict";

// ---------- 状态 ----------
const state = {
  me: null,          // {id, nickname}
  users: [],         // [{id, nickname, online}]
  inbox: [],
  outbox: [],
  activeTab: "inbox",
  es: null,          // EventSource
  pendingFile: null, // 待发送的 File 对象
};

const $ = (id) => document.getElementById(id);

// ---------- 工具 ----------
function fmtSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

function fmtTime(epochSec) {
  const d = new Date(epochSec * 1000);
  const diff = Date.now() - d.getTime();
  if (diff < 60 * 1000) return "刚刚";
  if (diff < 3600 * 1000) return Math.floor(diff / 60000) + " 分钟前";
  if (diff < 24 * 3600 * 1000) return Math.floor(diff / 3600000) + " 小时前";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtClock(epochSec) {
  const d = new Date(epochSec * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fileIcon(name, mime) {
  const ext = (name.split(".").pop() || "").toLowerCase();
  if ((mime || "").startsWith("image/") || ["jpg", "jpeg", "png", "gif", "webp", "svg", "heic"].includes(ext)) return "🖼️";
  if ((mime || "").startsWith("video/") || ["mp4", "mov", "mkv", "avi", "webm"].includes(ext)) return "🎬";
  if ((mime || "").startsWith("audio/") || ["mp3", "flac", "wav", "m4a", "aac"].includes(ext)) return "🎵";
  if (["zip", "rar", "7z", "tar", "gz"].includes(ext)) return "🗜️";
  if (["pdf"].includes(ext)) return "📕";
  if (["doc", "docx", "pages"].includes(ext)) return "📝";
  if (["xls", "xlsx", "numbers", "csv"].includes(ext)) return "📊";
  if (["ppt", "pptx", "key"].includes(ext)) return "📽️";
  if (["txt", "md", "log"].includes(ext)) return "📃";
  if (["app", "dmg", "pkg", "exe", "apk", "ipa"].includes(ext)) return "📦";
  if (["js", "py", "java", "go", "rs", "c", "cpp", "h", "html", "css", "json"].includes(ext)) return "💻";
  return "📄";
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text; // textContent 防 XSS
  return node;
}

// ---------- Toast ----------
function toast(message, type = "info") {
  const node = el("div", "toast " + type, message);
  $("toasts").appendChild(node);
  setTimeout(() => {
    node.classList.add("leaving");
    setTimeout(() => node.remove(), 300);
  }, 4000);
}

// ---------- API ----------
async function api(path, options = {}) {
  const resp = await fetch(path, options);
  if (resp.status === 401) {
    showSetup();
    throw new Error("未登录");
  }
  if (resp.status === 204) return null;
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || "请求失败（" + resp.status + "）");
  return data;
}

// ---------- 视图切换 ----------
function showSetup() {
  $("main-view").classList.add("hidden");
  $("setup-view").classList.remove("hidden");
  if (state.es) { state.es.close(); state.es = null; }
}

function showMain() {
  $("setup-view").classList.add("hidden");
  $("main-view").classList.remove("hidden");
}

// ---------- 初始化 ----------
async function init() {
  bindEvents();
  try {
    state.me = await api("/api/me");
  } catch {
    return; // 未登录，setup 视图已显示
  }
  await enterMain();
}

async function enterMain() {
  $("my-nickname").textContent = state.me.nickname;
  showMain();
  connectEvents();
  await Promise.all([refreshUsers(), refreshInbox(), refreshOutbox()]);
}

// ---------- 注册 ----------
async function register() {
  const input = $("nickname-input");
  const nickname = input.value.trim();
  $("setup-error").textContent = "";
  if (!nickname) {
    $("setup-error").textContent = "请输入昵称";
    return;
  }
  $("setup-btn").disabled = true;
  try {
    const resp = await fetch("/api/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nickname }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 409) {
      $("setup-error").textContent = "该昵称已被使用，请换一个";
      return;
    }
    if (!resp.ok) {
      $("setup-error").textContent = data.error || "注册失败";
      return;
    }
    state.me = data;
    await enterMain();
  } catch {
    $("setup-error").textContent = "网络错误，请重试";
  } finally {
    $("setup-btn").disabled = false;
  }
}

// ---------- SSE ----------
function connectEvents() {
  if (state.es) state.es.close();
  const es = new EventSource("/api/events");
  state.es = es;

  es.onopen = () => setConnStatus(true);
  es.onerror = () => setConnStatus(false); // 浏览器会自动重连

  es.addEventListener("file_received", (e) => {
    const d = JSON.parse(e.data);
    toast(`📥 ${d.from.nickname} 给你发送了《${d.name}》，请查收`, "success");
    refreshInbox();
  });

  es.addEventListener("file_downloaded", (e) => {
    const d = JSON.parse(e.data);
    toast(`✅ ${d.by.nickname} 于 ${fmtClock(d.downloaded_at)} 下载了《${d.name}》`);
    refreshOutbox();
  });

  es.addEventListener("file_deleted", (e) => {
    const d = JSON.parse(e.data);
    toast(`🗑 《${d.name}》已被发送者删除`);
    refreshInbox();
  });
}

function setConnStatus(online) {
  const dot = $("conn-dot");
  dot.classList.toggle("online", online);
  dot.classList.toggle("offline", !online);
  dot.title = online ? "已连接" : "连接中…";
}

// ---------- 数据刷新 ----------
async function refreshUsers() {
  try { state.users = await api("/api/users"); } catch { /* 忽略，下轮再试 */ }
}

async function refreshInbox() {
  try {
    state.inbox = await api("/api/files/inbox");
    updateBadge();
    if (state.activeTab === "inbox") renderInbox();
  } catch { /* 忽略 */ }
}

async function refreshOutbox() {
  try {
    state.outbox = await api("/api/files/outbox");
    if (state.activeTab === "outbox") renderOutbox();
  } catch { /* 忽略 */ }
}

function updateBadge() {
  const unread = state.inbox.filter((f) => !f.downloaded).length;
  const badge = $("inbox-badge");
  badge.textContent = unread;
  badge.classList.toggle("hidden", unread === 0);
  document.title = (unread > 0 ? `(${unread}) ` : "") + "局域网文件互传";
}

// ---------- 渲染 ----------
function renderList(items, renderCard, emptyEmoji, emptyText) {
  const list = $("file-list");
  list.textContent = "";
  if (!items.length) {
    const empty = el("div", "empty-state");
    empty.appendChild(el("span", "empty-emoji", emptyEmoji));
    empty.appendChild(el("div", null, emptyText));
    list.appendChild(empty);
    return;
  }
  items.forEach((item) => list.appendChild(renderCard(item)));
}

function renderInbox() {
  renderList(state.inbox, inboxCard, "📭", "暂无收到的文件\n点右下角 ＋ 给好友发送文件吧");
}

function renderOutbox() {
  renderList(state.outbox, outboxCard, "📤", "还没有发送过文件");
}

function inboxCard(f) {
  const card = el("div", "file-card");
  const main = el("div", "file-card-main");
  main.appendChild(el("span", "file-icon", fileIcon(f.original_name, f.mime)));

  const info = el("div", "file-info");
  info.appendChild(el("div", "file-name", f.original_name));
  const meta = el("div", "file-meta");
  meta.appendChild(el("span", null, `来自 ${f.uploader.nickname}`));
  meta.appendChild(el("span", null, "·"));
  meta.appendChild(el("span", null, fmtSize(f.size)));
  meta.appendChild(el("span", null, "·"));
  meta.appendChild(el("span", null, fmtTime(f.created_at)));
  info.appendChild(meta);
  main.appendChild(info);

  const actions = el("div", "file-actions");
  if (f.downloaded) {
    actions.appendChild(el("span", "tag-downloaded", "已下载 " + fmtClock(f.downloaded_at)));
  }
  const dl = el("a", "btn-download", "下载");
  dl.href = `/api/files/${f.id}/download`;
  dl.addEventListener("click", () => {
    // 浏览器直接下载，稍后刷新列表以更新"已下载"标记和角标
    setTimeout(refreshInbox, 1200);
  });
  actions.appendChild(dl);
  main.appendChild(actions);

  card.appendChild(main);
  return card;
}

function outboxCard(f) {
  const card = el("div", "file-card");
  const main = el("div", "file-card-main");
  main.appendChild(el("span", "file-icon", fileIcon(f.original_name, f.mime)));

  const info = el("div", "file-info");
  info.appendChild(el("div", "file-name", f.original_name));
  const meta = el("div", "file-meta");
  meta.appendChild(el("span", null, `发给 ${f.recipient.nickname}`));
  meta.appendChild(el("span", null, "·"));
  meta.appendChild(el("span", null, fmtSize(f.size)));
  meta.appendChild(el("span", null, "·"));
  meta.appendChild(el("span", null, fmtTime(f.created_at)));
  info.appendChild(meta);
  main.appendChild(info);

  const actions = el("div", "file-actions");
  const del = el("button", "btn-delete", "删除");
  del.addEventListener("click", () => deleteFile(f));
  actions.appendChild(del);
  main.appendChild(actions);
  card.appendChild(main);

  // 下载记录（可展开）
  const records = el("div", "download-records");
  const count = f.downloads.length;
  const toggle = el(
    "button",
    "download-records-toggle",
    count ? `📋 ${count} 次下载记录 ▾` : "📋 等待对方下载"
  );
  records.appendChild(toggle);
  if (count) {
    const listWrap = el("div", "download-records-list hidden");
    f.downloads.forEach((d) => {
      const row = el("div", "record");
      row.appendChild(el("span", null, d.downloader.nickname));
      row.appendChild(el("span", null, fmtTime(d.downloaded_at)));
      listWrap.appendChild(row);
    });
    toggle.addEventListener("click", () => listWrap.classList.toggle("hidden"));
    records.appendChild(listWrap);
  }
  card.appendChild(records);
  return card;
}

// ---------- 删除 ----------
async function deleteFile(f) {
  if (!confirm(`确定删除《${f.original_name}》吗？\n对方将无法再下载该文件。`)) return;
  try {
    await api(`/api/files/${f.id}`, { method: "DELETE" });
    toast("已删除");
    refreshOutbox();
  } catch (err) {
    toast(err.message, "error");
  }
}

// ---------- 上传 ----------
function pickFile() {
  $("file-input").click();
}

function onFilePicked(file) {
  if (!file) return;
  state.pendingFile = file;
  $("modal-file-icon").textContent = fileIcon(file.name, file.type);
  $("modal-file-name").textContent = file.name;
  $("modal-file-size").textContent = fmtSize(file.size);
  fillRecipientSelect();
  $("upload-progress-wrap").classList.add("hidden");
  $("upload-progress").style.width = "0%";
  $("upload-percent").textContent = "0%";
  $("upload-send").disabled = true;
  $("upload-modal").classList.remove("hidden");
  refreshUsers().then(fillRecipientSelect); // 打开弹窗时刷新在线状态
}

function fillRecipientSelect() {
  const select = $("recipient-select");
  const current = select.value;
  select.textContent = "";
  const placeholder = el("option", null, "请选择接收人");
  placeholder.disabled = true;
  if (!current) placeholder.selected = true;
  select.appendChild(placeholder);
  state.users
    .filter((u) => u.id !== state.me.id)
    .forEach((u) => {
      const opt = el("option", null, u.nickname + (u.online ? " · 在线" : ""));
      opt.value = u.id;
      select.appendChild(opt);
    });
  if (current) select.value = current;
  $("upload-send").disabled = !select.value;
}

function closeUploadModal() {
  $("upload-modal").classList.add("hidden");
  state.pendingFile = null;
  $("file-input").value = "";
}

function sendUpload() {
  const file = state.pendingFile;
  const recipientId = $("recipient-select").value;
  if (!file || !recipientId) return;

  const form = new FormData();
  form.append("file", file);
  form.append("recipient_id", recipientId);

  const recipient = state.users.find((u) => String(u.id) === String(recipientId));

  $("upload-send").disabled = true;
  $("upload-cancel").disabled = true;
  $("upload-progress-wrap").classList.remove("hidden");

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/files");

  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      $("upload-progress").style.width = pct + "%";
      $("upload-percent").textContent = pct + "%";
    }
  };

  xhr.onload = () => {
    $("upload-cancel").disabled = false;
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch { /* 忽略 */ }
    if (xhr.status === 201) {
      closeUploadModal();
      toast(`已发送给 ${recipient ? recipient.nickname : "对方"}`, "success");
      switchTab("outbox");
      refreshOutbox();
    } else if (xhr.status === 401) {
      closeUploadModal();
      showSetup();
    } else {
      $("upload-send").disabled = false;
      toast(data.error || "发送失败", "error");
    }
  };

  xhr.onerror = () => {
    $("upload-cancel").disabled = false;
    $("upload-send").disabled = false;
    toast("网络错误，发送失败", "error");
  };

  xhr.send(form);
}

// ---------- Tab ----------
function switchTab(tab) {
  state.activeTab = tab;
  $("tab-inbox").classList.toggle("active", tab === "inbox");
  $("tab-outbox").classList.toggle("active", tab === "outbox");
  if (tab === "inbox") renderInbox();
  else renderOutbox();
}

// ---------- 事件绑定 ----------
function bindEvents() {
  $("setup-btn").addEventListener("click", register);
  $("nickname-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") register();
  });

  $("tab-inbox").addEventListener("click", () => switchTab("inbox"));
  $("tab-outbox").addEventListener("click", () => switchTab("outbox"));

  $("fab").addEventListener("click", pickFile);
  $("file-input").addEventListener("change", (e) => onFilePicked(e.target.files[0]));
  $("modal-repick").addEventListener("click", pickFile);
  $("upload-cancel").addEventListener("click", closeUploadModal);
  $("upload-send").addEventListener("click", sendUpload);
  $("recipient-select").addEventListener("change", () => {
    $("upload-send").disabled = !$("recipient-select").value;
  });

  // 整页拖拽上传
  let dragDepth = 0;
  window.addEventListener("dragenter", (e) => {
    e.preventDefault();
    if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) {
      dragDepth++;
      $("drop-overlay").classList.remove("hidden");
    }
  });
  window.addEventListener("dragleave", (e) => {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) $("drop-overlay").classList.add("hidden");
  });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    $("drop-overlay").classList.add("hidden");
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file && !$("main-view").classList.contains("hidden")) onFilePicked(file);
  });

  // 回到前台：SSE 断开则重连并全量刷新（覆盖手机后台断连场景）
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible" || !state.me) return;
    if (!state.es || state.es.readyState === EventSource.CLOSED) {
      connectEvents();
    }
    refreshUsers();
    refreshInbox();
    refreshOutbox();
  });

  // 60 秒静默轮询兜底（弥补极端情况下丢失的 SSE 事件）
  setInterval(() => {
    if (!state.me || $("main-view").classList.contains("hidden")) return;
    if (state.activeTab === "inbox") refreshInbox();
    else refreshOutbox();
  }, 60000);
}

init();
