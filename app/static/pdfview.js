// 站內 PDF 檢視器（pdf.js）：手機、平板也能清楚看 PDF。
// - 預設「符合寬度」，可放大縮小（按鈕、雙指縮放、Ctrl+滾輪），放大後重新以實際解析度繪製，不會糊。
// - 只繪製畫面附近的頁面，離開很遠的頁面釋放記憶體（30 頁以上的日報在手機上也順）。
// - 目錄（PDF 書籤）、跳頁、下載。
import * as pdfjsLib from "./vendor/pdfjs/pdf.min.js";

const root = document.getElementById("pdfv");
const base = root.dataset.base; // static/vendor/pdfjs/
pdfjsLib.GlobalWorkerOptions.workerSrc = base + "pdf.worker.min.js";

const pagesEl = root.querySelector(".pdfv-pages");
const scroller = root.querySelector(".pdfv-scroll");
const pageNow = root.querySelector("[data-page-now]");
const pageTotal = root.querySelector("[data-page-total]");
const zoomLabel = root.querySelector("[data-zoom]");
const tocEl = root.querySelector(".pdfv-toc");
const statusEl = root.querySelector(".pdfv-status");

let doc = null;
let sizes = []; // 每頁原始尺寸（pt）
let fitScale = 1; // 符合寬度時的比例
let zoom = 1; // 相對符合寬度的倍率
const MIN_ZOOM = 0.5, MAX_ZOOM = 5;
const rendered = new Map(); // 頁碼 -> {scale, task}
const dpr = () => Math.min(window.devicePixelRatio || 1, 3);

function setStatus(msg, err) {
  statusEl.textContent = msg || "";
  statusEl.hidden = !msg;
  statusEl.classList.toggle("err", !!err);
}

function scale() { return fitScale * zoom; }

function computeFit() {
  const w = scroller.clientWidth - (window.innerWidth < 700 ? 12 : 40);
  const maxW = Math.max(...sizes.map((s) => s.w));
  fitScale = Math.max(0.2, w / maxW);
}

function layout() {
  const s = scale();
  pagesEl.querySelectorAll(".pdfv-page").forEach((el, i) => {
    el.style.width = Math.floor(sizes[i].w * s) + "px";
    el.style.height = Math.floor(sizes[i].h * s) + "px";
  });
  zoomLabel.textContent = Math.round(zoom * 100) + "%";
}

function release(n) {
  const r = rendered.get(n);
  if (!r) return;
  if (r.task) r.task.cancel();
  const c = pagesEl.children[n - 1].querySelector("canvas");
  if (c) { c.width = 0; c.height = 0; c.remove(); }
  rendered.delete(n);
}

async function render(n) {
  const s = scale();
  const r = rendered.get(n);
  if (r && r.scale === s) return;
  const holder = pagesEl.children[n - 1];
  const page = await doc.getPage(n);
  const vp = page.getViewport({ scale: s * dpr() });
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(vp.width);
  canvas.height = Math.floor(vp.height);
  const entry = { scale: s, task: null };
  if (r && r.task) r.task.cancel();
  rendered.set(n, entry);
  entry.task = page.render({ canvasContext: canvas.getContext("2d"), viewport: vp });
  try {
    await entry.task.promise;
  } catch (e) {
    if (e && e.name === "RenderingCancelledException") return;
    throw e;
  }
  entry.task = null;
  if (rendered.get(n) !== entry) return;
  const old = holder.querySelector("canvas");
  if (old) { old.width = 0; old.height = 0; old.remove(); }
  holder.appendChild(canvas);
  holder.classList.add("done");
}

// 目前頁碼與需要繪製的頁面
let ticking = false;
function onScroll() {
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(() => {
    ticking = false;
    const top = scroller.scrollTop, bottom = top + scroller.clientHeight;
    const mid = top + scroller.clientHeight / 3;
    let current = 1;
    const want = new Set();
    Array.from(pagesEl.children).forEach((el, i) => {
      const a = el.offsetTop, b = a + el.offsetHeight;
      if (a <= mid) current = i + 1;
      if (b > top - scroller.clientHeight && a < bottom + scroller.clientHeight) want.add(i + 1);
    });
    pageNow.value = current;
    want.forEach((n) => render(n).catch((e) => setStatus("第 " + n + " 頁無法顯示：" + e.message, true)));
    for (const n of Array.from(rendered.keys())) {
      if (!want.has(n) && Math.abs(n - current) > 4) release(n);
    }
  });
}

function goPage(n) {
  n = Math.min(Math.max(1, n | 0), doc.numPages);
  scroller.scrollTop = pagesEl.children[n - 1].offsetTop - 8;
  onScroll();
}

function setZoom(z, anchor) {
  z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
  if (Math.abs(z - zoom) < 0.001) return;
  // 以 anchor（畫面上的點）為中心縮放，畫面不會跳走
  const ax = anchor ? anchor.x : scroller.clientWidth / 2;
  const ay = anchor ? anchor.y : scroller.clientHeight / 3;
  const ratio = z / zoom;
  const cx = scroller.scrollLeft + ax, cy = scroller.scrollTop + ay;
  zoom = z;
  layout();
  scroller.scrollLeft = cx * ratio - ax;
  scroller.scrollTop = cy * ratio - ay;
  rendered.forEach((r) => { r.scale = -1; }); // 下次捲動時以新解析度重畫（舊圖先拉伸顯示）
  onScroll();
}

// 雙指縮放：手勢進行中只用 CSS 放大，放開後重畫
let pinch = null;
scroller.addEventListener("touchstart", (e) => {
  if (e.touches.length === 2) {
    const [a, b] = e.touches;
    const rect = scroller.getBoundingClientRect();
    pinch = { d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY), z: zoom, k: 1,
              x: (a.clientX + b.clientX) / 2 - rect.left, y: (a.clientY + b.clientY) / 2 - rect.top };
  }
}, { passive: true });
scroller.addEventListener("touchmove", (e) => {
  if (!pinch || e.touches.length !== 2) return;
  e.preventDefault();
  const [a, b] = e.touches;
  const k = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) / pinch.d;
  pinch.k = Math.min(MAX_ZOOM / pinch.z, Math.max(MIN_ZOOM / pinch.z, k));
  pagesEl.style.transformOrigin = (scroller.scrollLeft + pinch.x) + "px " + (scroller.scrollTop + pinch.y) + "px";
  pagesEl.style.transform = "scale(" + pinch.k + ")";
}, { passive: false });
scroller.addEventListener("touchend", (e) => {
  if (!pinch || e.touches.length >= 2) return;
  const p = pinch;
  pinch = null;
  pagesEl.style.transform = "";
  setZoom(p.z * p.k, { x: p.x, y: p.y });
});
scroller.addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  const rect = scroller.getBoundingClientRect();
  setZoom(zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1), { x: e.clientX - rect.left, y: e.clientY - rect.top });
}, { passive: false });

root.querySelector("[data-act=in]").addEventListener("click", () => setZoom(zoom * 1.25));
root.querySelector("[data-act=out]").addEventListener("click", () => setZoom(zoom / 1.25));
root.querySelector("[data-act=fit]").addEventListener("click", () => setZoom(1));
root.querySelector("[data-act=prev]").addEventListener("click", () => goPage((pageNow.value | 0) - 1));
root.querySelector("[data-act=next]").addEventListener("click", () => goPage((pageNow.value | 0) + 1));
pageNow.addEventListener("change", () => goPage(pageNow.value));
scroller.addEventListener("scroll", onScroll, { passive: true });
const tocBtn = root.querySelector("[data-act=toc]");
tocBtn.addEventListener("click", () => {
  const open = tocEl.hidden;
  tocEl.hidden = !open;
  tocBtn.setAttribute("aria-expanded", String(open));
});
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (!doc) return;
    const cur = pageNow.value | 0;
    computeFit();
    layout();
    rendered.forEach((r) => { r.scale = -1; });
    goPage(cur);
  }, 150);
});

async function buildToc() {
  const outline = await doc.getOutline();
  if (!outline || !outline.length) { tocBtn.hidden = true; return; }
  const ul = document.createElement("ul");
  async function add(items, parent) {
    for (const it of items) {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = it.title;
      btn.addEventListener("click", async () => {
        let dest = it.dest;
        if (typeof dest === "string") dest = await doc.getDestination(dest);
        if (!dest) return;
        const idx = await doc.getPageIndex(dest[0]);
        if (window.innerWidth < 900) { tocEl.hidden = true; tocBtn.setAttribute("aria-expanded", "false"); }
        goPage(idx + 1);
      });
      li.appendChild(btn);
      if (it.items && it.items.length) {
        const sub = document.createElement("ul");
        await add(it.items, sub);
        li.appendChild(sub);
      }
      parent.appendChild(li);
    }
  }
  await add(outline, ul);
  tocEl.querySelector(".pdfv-toc-list").appendChild(ul);
}

(async function main() {
  setStatus("載入中…");
  try {
    doc = await pdfjsLib.getDocument({
      url: root.dataset.src,
      standardFontDataUrl: base + "standard_fonts/",
      isEvalSupported: false,
      withCredentials: true,
    }).promise;
  } catch (e) {
    setStatus(e && e.status === 409 ? "正式 PDF 產生中，請稍後再開。" : "PDF 載入失敗：" + (e.message || e), true);
    return;
  }
  for (let i = 1; i <= doc.numPages; i++) {
    const vp = (await doc.getPage(i)).getViewport({ scale: 1 });
    sizes.push({ w: vp.width, h: vp.height });
    const div = document.createElement("div");
    div.className = "pdfv-page";
    div.dataset.n = i;
    pagesEl.appendChild(div);
  }
  pageTotal.textContent = doc.numPages;
  pageNow.max = doc.numPages;
  computeFit();
  layout();
  setStatus("");
  onScroll();
  buildToc().catch(() => { tocBtn.hidden = true; });
})();
