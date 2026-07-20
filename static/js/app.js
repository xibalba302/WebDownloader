const I18N = {
  en: {
    title: "WebDownloader",
    subtitle: "Download a full website and browse it offline — all links, images, styles and fonts rewritten to work without internet.",
    labelUrl: "Website URL",
    labelMaxPages: "Max pages",
    hintPages: "(0 = unlimited)",
    labelMaxDepth: "Max link depth",
    hintDepth: "(-1 = unlimited)",
    labelSameDomain: "Stay on the same domain",
    labelRobots: "Respect robots.txt",
    startBtn: "Start download",
    hint: "Only download sites you own or have permission to archive.",
    progressTitle: "Progress",
    statPages: "pages",
    statAssets: "assets",
    statErrors: "errors",
    cancelBtn: "Cancel",
    browseBtn: "Browse offline copy",
    downloadBtn: "Download ZIP",
    langBtn: "العربية",
    invalidUrl: "Please enter a valid URL.",
    startFailed: "Could not start the download.",
    savedTo: "Saved to:",
    copyBtn: "Copy",
    copiedBtn: "Copied!",
  },
  ar: {
    title: "ويب داونلودر",
    subtitle: "نزّل موقعاً كاملاً وتصفّحه دون اتصال بالإنترنت — تتم إعادة كتابة كل الروابط والصور والأنماط والخطوط لتعمل بلا اتصال.",
    labelUrl: "رابط الموقع",
    labelMaxPages: "الحد الأقصى للصفحات",
    hintPages: "(0 = بلا حدود)",
    labelMaxDepth: "أقصى عمق للروابط",
    hintDepth: "(-1 = بلا حدود)",
    labelSameDomain: "الالتزام بنفس النطاق",
    labelRobots: "احترام ملف robots.txt",
    startBtn: "بدء التنزيل",
    hint: "لا تُنزّل إلا المواقع التي تملكها أو لديك إذن بأرشفتها.",
    progressTitle: "التقدّم",
    statPages: "صفحات",
    statAssets: "ملفات",
    statErrors: "أخطاء",
    cancelBtn: "إلغاء",
    browseBtn: "تصفح النسخة غير المتصلة",
    downloadBtn: "تنزيل الأرشيف (ZIP)",
    langBtn: "English",
    invalidUrl: "الرجاء إدخال رابط صحيح.",
    startFailed: "تعذّر بدء التنزيل.",
    savedTo: "مكان الحفظ:",
    copyBtn: "نسخ",
    copiedBtn: "تم النسخ!",
  },
};

let currentLang = localStorage.getItem("wd_lang") || "en";
let pollTimer = null;

function applyLang(lang) {
  currentLang = lang;
  localStorage.setItem("wd_lang", lang);
  const html = document.getElementById("html-root");
  html.setAttribute("lang", lang);
  html.setAttribute("dir", lang === "ar" ? "rtl" : "ltr");
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    if (I18N[lang][key] !== undefined) el.textContent = I18N[lang][key];
  });
  document.getElementById("lang-toggle").textContent = I18N[lang].langBtn;
}

document.getElementById("lang-toggle").addEventListener("click", () => {
  applyLang(currentLang === "en" ? "ar" : "en");
});

applyLang(currentLang);

const form = document.getElementById("job-form");
const startBtn = document.getElementById("start-btn");
const progressCard = document.getElementById("progress-card");
const statusBadge = document.getElementById("status-badge");
const pagesDone = document.getElementById("pages-done");
const assetsDone = document.getElementById("assets-done");
const errorsCount = document.getElementById("errors-count");
const currentAction = document.getElementById("current-action");
const logBox = document.getElementById("log-box");
const cancelBtn = document.getElementById("cancel-btn");
const browseLink = document.getElementById("browse-link");
const downloadLink = document.getElementById("download-link");
const folderPathRow = document.getElementById("folder-path-row");
const folderPathText = document.getElementById("folder-path-text");
const copyPathBtn = document.getElementById("copy-path-btn");

let activeJobId = null;

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = document.getElementById("url").value.trim();
  if (!url) {
    alert(I18N[currentLang].invalidUrl);
    return;
  }

  const payload = {
    url,
    max_pages: document.getElementById("max_pages").value,
    max_depth: document.getElementById("max_depth").value,
    same_domain_only: document.getElementById("same_domain_only").checked,
    respect_robots: document.getElementById("respect_robots").checked,
  };

  startBtn.disabled = true;
  try {
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      alert(data.error || I18N[currentLang].startFailed);
      startBtn.disabled = false;
      return;
    }
    activeJobId = data.job_id;
    progressCard.classList.remove("hidden");
    browseLink.classList.add("hidden");
    downloadLink.classList.add("hidden");
    folderPathRow.classList.add("hidden");
    cancelBtn.classList.remove("hidden");
    logBox.textContent = "";
    startPolling();
  } catch (err) {
    alert(I18N[currentLang].startFailed);
    startBtn.disabled = false;
  }
});

cancelBtn.addEventListener("click", async () => {
  if (!activeJobId) return;
  await fetch(`/api/jobs/${activeJobId}/cancel`, { method: "POST" });
});

copyPathBtn.addEventListener("click", async () => {
  const text = folderPathText.textContent;
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch (err) {
    // Clipboard API unavailable (e.g. no HTTPS/permission) - fall back silently.
  }
  const original = copyPathBtn.textContent;
  copyPathBtn.textContent = I18N[currentLang].copiedBtn;
  setTimeout(() => {
    copyPathBtn.textContent = original;
  }, 1500);
});

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(fetchStatus, 900);
  fetchStatus();
}

async function fetchStatus() {
  if (!activeJobId) return;
  const res = await fetch(`/api/jobs/${activeJobId}`);
  if (!res.ok) return;
  const data = await res.json();
  render(data);
  if (["completed", "error", "cancelled"].includes(data.status)) {
    clearInterval(pollTimer);
    pollTimer = null;
    startBtn.disabled = false;
    cancelBtn.classList.add("hidden");
    if (data.status === "completed") {
      browseLink.href = `/site/${activeJobId}/`;
      browseLink.classList.remove("hidden");
      if (data.zip_ready) {
        downloadLink.href = `/api/jobs/${activeJobId}/download`;
        downloadLink.classList.remove("hidden");
      }
      if (data.output_dir) {
        folderPathText.textContent = data.output_dir;
        folderPathRow.classList.remove("hidden");
      }
    }
  }
}

function render(data) {
  statusBadge.textContent = data.status;
  statusBadge.className = "badge " + data.status;
  pagesDone.textContent = data.pages_done;
  assetsDone.textContent = data.assets_done;
  errorsCount.textContent = data.errors_count;
  currentAction.textContent = data.current_action || "";
  logBox.textContent = (data.log || []).join("\n");
  logBox.scrollTop = logBox.scrollHeight;
  if (data.error_message) {
    currentAction.textContent = data.error_message;
  }
}
