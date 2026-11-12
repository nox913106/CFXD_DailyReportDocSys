(function () {
  "use strict";

  var csrfToken = (document.querySelector('meta[name="csrf-token"]') || {}).content;

  function api(url, options) {
    options = options || {};
    options.headers = Object.assign({ "X-CSRFToken": csrfToken }, options.headers || {});
    options.credentials = "same-origin";
    return fetch(url, options).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) throw new Error(data.error || "操作失敗（HTTP " + resp.status + "）");
        return data;
      });
    });
  }

  // ---------- 可增減的記錄 / 表格列 ----------

  function cloneTemplate(tpl, container, index) {
    var next = parseInt(container.dataset.next || "0", 10);
    if (index === undefined) index = next;
    container.dataset.next = Math.max(next, index + 1);
    var html = tpl.innerHTML.replace(/__i__/g, String(index));
    container.insertAdjacentHTML("beforeend", html);
    markDirty(container);
    renumberRows(container.closest("table"));
  }

  // Alarm Summary 的 No. 欄依畫面順序重新編號
  function renumberRows(table) {
    if (!table) return;
    var n = 0;
    table.querySelectorAll("tbody tr [data-row-no]").forEach(function (el) { el.textContent = String(++n); });
  }

  document.addEventListener("click", function (e) {
    var t = e.target;
    if (t.matches("[data-add-record]")) {
      var prefix = t.dataset.addRecord;
      var container = document.querySelector('.record-container[data-prefix="' + prefix + '"]');
      cloneTemplate(document.querySelector('template[data-record-template="' + prefix + '"]'), container);
      var hint = container.parentElement.querySelector(".empty-hint");
      if (hint) hint.hidden = true;
    } else if (t.matches("[data-remove-record]")) {
      markDirty(t);
      t.closest(".record").remove();
    } else if (t.matches("[data-add-row]")) {
      var name = t.dataset.addRow;
      var table = document.querySelector('[data-row-container="' + name + '"]');
      var tbody = table.tBodies[0];
      tbody.dataset.next = table.dataset.next;
      cloneTemplate(document.querySelector('template[data-row-template="' + name + '"]'), tbody);
      table.dataset.next = tbody.dataset.next;
    } else if (t.matches("[data-remove-row]")) {
      markDirty(t);
      var tbl = t.closest("table");
      t.closest("tr").remove();
      renumberRows(tbl);
    } else if (t.matches("[data-row-move]")) {
      var tr = t.closest("tr");
      var sib = t.dataset.rowMove === "up" ? tr.previousElementSibling : tr.nextElementSibling;
      if (!sib) return;
      if (t.dataset.rowMove === "up") tr.parentNode.insertBefore(tr, sib);
      else tr.parentNode.insertBefore(sib, tr);
      markDirty(tr);
      renumberRows(tr.closest("table"));
      t.focus();
    } else if (t.matches("[data-delete-image]")) {
      deleteImage(t.closest(".thumb"));
    } else if (t.matches("[data-requeue-ocr]")) {
      if (!confirm("重新辨識這份日報的所有截圖日期？")) return;
      api(t.dataset.requeueOcr, { method: "POST" })
        .then(function () { location.reload(); })
        .catch(function (err) { alert(err.message); });
    } else if (t.matches("[data-move]")) {
      moveImage(t.closest(".thumb"), t.dataset.move);
    }
  });

  // ---------- 未儲存提醒 ----------

  var dirtyForms = new Set();
  function markDirty(el) {
    if (!el.closest || el.closest(".image-zone")) return;  // 照片上傳後立即存檔，不算未儲存
    var form = el.closest("form[data-dirty-guard]");
    if (form) {
      dirtyForms.add(form);
      form.dispatchEvent(new CustomEvent("dirty"));
    }
  }
  document.addEventListener("input", function (e) { markDirty(e.target); });
  document.addEventListener("change", function (e) { markDirty(e.target); });
  document.addEventListener("submit", function (e) { dirtyForms.delete(e.target); dirtyForms.clear(); });
  window.addEventListener("beforeunload", function (e) {
    if (dirtyForms.size) { e.preventDefault(); e.returnValue = ""; }
  });

  // ---------- 拖曳調整順序（table[data-sortable]：拖曳每列的 ⠿） ----------

  // 用 pointer 事件（滑鼠、觸控、觸控筆都可以），不用 HTML5 drag & drop：表格列在部分瀏覽器拖不動
  var dragRow = null;
  document.addEventListener("pointerdown", function (e) {
    var h = e.target.closest && e.target.closest("table[data-sortable] .drag-handle");
    if (!h || e.button > 0) return;
    e.preventDefault();
    dragRow = h.closest("tr");
    dragRow.classList.add("dragging");
    document.body.classList.add("sorting");
  });
  document.addEventListener("pointermove", function (e) {
    if (!dragRow) return;
    e.preventDefault();
    var body = dragRow.parentNode;
    Array.prototype.some.call(body.children, function (tr) {
      if (tr === dragRow) return false;
      var box = tr.getBoundingClientRect();
      if (e.clientY < box.top || e.clientY > box.bottom) return false;
      body.insertBefore(dragRow, e.clientY > box.top + box.height / 2 ? tr.nextSibling : tr);
      return true;
    });
  });
  function endDrag() {
    if (!dragRow) return;
    var tr = dragRow;
    dragRow = null;
    tr.classList.remove("dragging");
    document.body.classList.remove("sorting");
    markDirty(tr);
    renumberRows(tr.closest("table"));
  }
  document.addEventListener("pointerup", endDrag);
  document.addEventListener("pointercancel", endDrag);

  // ---------- 自動儲存（form[data-autosave]）----------
  // 1. 每分鐘：有修改就在背景送出（伺服器回 JSON 與新的 CSRF token，頁面開再久也不會過期）。
  // 2. 每次輸入：內容另存一份在這台電腦的瀏覽器（localStorage）。登入逾時、網路中斷、
  //    送出時 token 過期時，回到這一頁可以「還原」。成功存到伺服器後刪除。

  var AUTOSAVE_MS = 60000;

  function setCsrf(token) {
    if (!token) return;
    csrfToken = token;
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) meta.content = token;
    document.querySelectorAll('input[name="csrf_token"]').forEach(function (i) { i.value = token; });
  }

  function storage() {
    try { var k = "__t"; localStorage.setItem(k, k); localStorage.removeItem(k); return localStorage; }
    catch (e) { return null; }
  }

  function serialize(form) {
    var out = [];
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || el.name === "csrf_token" || el.type === "file" || el.disabled || el.closest("template")) return;
      if (el.type === "radio" || el.type === "checkbox") { if (el.checked) out.push([el.name, el.value, 1]); }
      else out.push([el.name, el.value]);
    });
    return out;
  }

  function rowOf(form, prefix, i) {
    var el = form.querySelector('[name^="' + prefix + "-" + i + '-"]');
    return el ? el.closest(".record, tr") : null;
  }

  function restore(form, fields) {
    // 新增過的記錄 / 列先建立，刪除過的移除，再依暫存的順序排列
    var wanted = {};
    fields.forEach(function (f) {
      var m = /^([a-z]+)-(\d+)-/.exec(f[0]);
      if (!m) return;
      (wanted[m[1]] = wanted[m[1]] || []);
      if (wanted[m[1]].indexOf(+m[2]) < 0) wanted[m[1]].push(+m[2]);
    });
    Object.keys(wanted).forEach(function (prefix) {
      var tpl = form.querySelector('template[data-record-template="' + prefix + '"], template[data-row-template="' + prefix + '"]');
      var box = form.querySelector('.record-container[data-prefix="' + prefix + '"]') ||
        (form.querySelector('[data-row-container="' + prefix + '"]') || {}).tBodies;
      if (box && box.length !== undefined) box = box[0];
      if (!tpl || !box) return;
      var table = box.closest("table");
      if (table) box.dataset.next = Math.max(+(box.dataset.next || 0), +(table.dataset.next || 0));
      wanted[prefix].forEach(function (i) { if (!rowOf(form, prefix, i)) cloneTemplate(tpl, box, i); });
      Array.prototype.slice.call(box.children).forEach(function (row) {
        var el = row.querySelector("[name]"), m = el && /^([a-z]+)-(\d+)-/.exec(el.name);
        if (m && wanted[prefix].indexOf(+m[2]) < 0) row.remove();
      });
      wanted[prefix].forEach(function (i) { var r = rowOf(form, prefix, i); if (r) box.appendChild(r); });
      var tbl = box.closest("table");
      if (tbl) { tbl.dataset.next = box.dataset.next; renumberRows(tbl); }
      var hint = box.parentElement.querySelector(".empty-hint");
      if (hint) hint.hidden = box.children.length > 0;
    });
    Array.prototype.forEach.call(form.elements, function (el) {
      if (el.type === "radio" || el.type === "checkbox") if (el.name) el.checked = false;
    });
    fields.forEach(function (f) {
      var els = form.querySelectorAll('[name="' + f[0].replace(/"/g, '\\"') + '"]');
      els.forEach(function (el) {
        if (el.type === "radio" || el.type === "checkbox") { if (el.value === f[1]) el.checked = true; }
        else el.value = f[1];
      });
    });
    markDirty(form.querySelector("[name]") || form);
    form.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function setupAutosave(form) {
    var store = storage();
    var key = "dr-draft:" + location.pathname;
    var status = form.querySelector("[data-autosave-status]");
    var version = 0, savedVersion = 0, busy = null, timer = null, localTimer = null;

    function show(text, cls) {
      if (!status) return;
      status.textContent = text;
      status.className = "autosave-status hint " + (cls || "");
    }

    function keepLocal() {
      if (!store || !dirtyForms.has(form)) return;  // 沒有未儲存的修改就不暫存
      try {
        store.setItem(key, JSON.stringify({ t: Date.now(), base: form.dataset.updated || "", fields: serialize(form) }));
      } catch (e) { /* 空間不足時略過 */ }
    }
    function dropLocal() { if (store) store.removeItem(key); }

    function post(retry) {
      var fd = new FormData(form);
      fd.set("csrf_token", csrfToken);
      return fetch(form.getAttribute("action") || location.href, {
        method: "POST", body: fd, credentials: "same-origin",
        headers: { "X-Autosave": "1", "X-CSRFToken": csrfToken, "Accept": "application/json" },
      }).then(function (resp) {
        var json = (resp.headers.get("Content-Type") || "").indexOf("application/json") >= 0;
        if (!json) throw new Error(resp.redirected || resp.status === 401 ? "login" : "HTTP " + resp.status);
        return resp.json().then(function (data) {
          if (data.csrf) setCsrf(data.csrf);
          if (data.error === "csrf" && !retry) {
            return fetch("/csrf-token", { credentials: "same-origin", headers: { "Accept": "application/json" } })
              .then(function (r) { if (r.status === 401) throw new Error("login"); return r.json(); })
              .then(function (d) { setCsrf(d.csrf); return post(true); });
          }
          if (!data.ok) throw new Error(data.error === "csrf" ? "login" : (data.error || "HTTP " + resp.status));
          return data;
        });
      });
    }

    function save() {
      if (busy) return busy;
      if (!dirtyForms.has(form) || savedVersion === version) return Promise.resolve(null);
      var v = version;
      show("自動儲存中…");
      busy = post(false).then(function (data) {
        savedVersion = v;
        if (data.updated) form.dataset.updated = data.updated;  // 自己存的不算「別人更新過」
        if (version === v) { dirtyForms.delete(form); clearTimeout(localTimer); dropLocal(); }
        show("已自動儲存 " + data.saved_at, "ok-text");
        return data;
      }).catch(function (err) {
        keepLocal();
        show(err.message === "login"
          ? "登入已逾時，內容暫存在這台電腦；請在新分頁重新登入，這頁會繼續自動儲存"
          : "自動儲存失敗（" + err.message + "），內容暫存在這台電腦，稍後再試", "fail-text");
        throw err;
      }).finally(function () { busy = null; });
      return busy;
    }
    form.autosave = function () { return save().catch(function () { return null; }); };

    form.addEventListener("dirty", function () {
      version++;
      clearTimeout(localTimer);
      localTimer = setTimeout(keepLocal, 1500);
      if (!timer) show("有未儲存的修改，1 分鐘內自動儲存");
    });
    timer = setInterval(function () { save().catch(function () {}); }, AUTOSAVE_MS);
    form.addEventListener("submit", function () { keepLocal(); clearInterval(timer); });
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") { keepLocal(); save().catch(function () {}); }
    });

    // 進入頁面：剛按儲存成功 → 刪除暫存；還有暫存 → 提示還原
    if (!store) return;
    var raw = store.getItem(key), draft = null;
    try { draft = raw && JSON.parse(raw); } catch (e) { draft = null; }
    if (!draft) return;
    if (document.querySelector(".flash.success")) { dropLocal(); return; }
    var bar = document.createElement("div");
    bar.className = "flash warn autosave-restore";
    var when = new Date(draft.t);
    var newer = draft.base && form.dataset.updated && form.dataset.updated > draft.base;
    bar.innerHTML = "這一頁有尚未儲存到系統的內容（暫存於 " + when.toLocaleString() + "）" +
      (newer ? "；之後系統上的內容有人更新過，還原會覆蓋那些更新" : "") + "。 ";
    var ok = document.createElement("button");
    ok.type = "button"; ok.textContent = "還原";
    var no = document.createElement("button");
    no.type = "button"; no.className = "secondary"; no.textContent = "捨棄";
    ok.addEventListener("click", function () {
      restore(form, draft.fields || []);
      bar.remove();
      show("已還原暫存的內容，1 分鐘內自動儲存（或按儲存）");
    });
    no.addEventListener("click", function () { if (confirm("捨棄暫存的內容？")) { dropLocal(); bar.remove(); } });
    bar.appendChild(ok); bar.appendChild(document.createTextNode(" ")); bar.appendChild(no);
    form.parentNode.insertBefore(bar, form);
  }
  document.querySelectorAll("form[data-autosave]").forEach(setupAutosave);

  // 「送出審核」前先把未儲存的修改存起來，避免送出的是舊內容
  document.querySelectorAll('form[action$="/submit"]').forEach(function (f) {
    f.addEventListener("submit", function (e) {
      var pending = Array.prototype.filter.call(document.querySelectorAll("form[data-autosave]"), function (x) {
        return dirtyForms.has(x) && x.autosave;
      });
      if (e.defaultPrevented || !pending.length || f.dataset.flushed) return;  // 確認視窗按了取消
      e.preventDefault();
      Promise.all(pending.map(function (x) { return x.autosave(); })).then(function (res) {
        if (res.some(function (r) { return !r; })) {
          alert("未儲存的修改存不進去（見頁面下方的訊息），請先處理後再送審。");
          return;
        }
        dirtyForms.clear();
        f.dataset.flushed = "1";
        f.submit();
      });
    });
  });

  // ---------- 檢查表進度 ----------

  var progress = document.querySelector("[data-checklist-progress]");
  function updateProgress() {
    var rows = document.querySelectorAll(".checklist tbody tr");
    var done = 0;
    rows.forEach(function (row) {
      var checked = row.querySelector('input[type="radio"]:checked');
      row.className = "result-" + (checked ? checked.value : "none");
      if (checked) done++;
    });
    progress.textContent = "已勾選 " + done + " / " + rows.length;
    progress.classList.toggle("complete", done === rows.length);
  }
  if (progress) {
    updateProgress();
    document.addEventListener("change", function (e) {
      if (e.target.matches('.checklist input[type="radio"]')) updateProgress();
    });
  }

  // ---------- 圖片上傳 ----------

  function thumbHtml(img) {
    var fig = document.createElement("figure");
    fig.className = "thumb";
    fig.dataset.url = img.url;
    var a = document.createElement("a");
    a.href = img.url;
    a.target = "_blank";
    var image = document.createElement("img");
    image.src = img.url;
    image.alt = img.name || "";
    a.appendChild(image);
    fig.appendChild(a);
    var cap = document.createElement("figcaption");
    cap.innerHTML =
      '<button type="button" class="link" data-move="up" title="往前">◀</button>' +
      '<button type="button" class="link" data-move="down" title="往後">▶</button>' +
      '<button type="button" class="link danger" data-delete-image>刪除</button>';
    fig.appendChild(cap);
    return fig;
  }

  function upload(zone, files) {
    var images = Array.prototype.filter.call(files, function (f) { return f.type.indexOf("image/") === 0; });
    if (!images.length) return;
    var fd = new FormData();
    fd.append("kind", zone.dataset.kind);
    if (zone.dataset.sectionId) fd.append("section_id", zone.dataset.sectionId);
    images.forEach(function (f, i) {
      // 從剪貼簿貼上的圖片沒有檔名
      fd.append("files", f, f.name || "paste-" + Date.now() + "-" + i + ".png");
    });
    zone.classList.add("busy");
    api(zone.dataset.uploadUrl, { method: "POST", body: fd })
      .then(function (data) {
        var thumbs = zone.querySelector(".thumbs");
        (data.images || []).forEach(function (img) { thumbs.appendChild(thumbHtml(img)); });
        if (data.errors && data.errors.length) alert(data.errors.join("\n"));
      })
      .catch(function (err) { alert(err.message); })
      .finally(function () { zone.classList.remove("busy"); });
  }

  function deleteImage(fig) {
    if (!confirm("確定刪除這張圖片？")) return;
    api(fig.dataset.url, { method: "DELETE" })
      .then(function () { fig.remove(); })
      .catch(function (err) { alert(err.message); });
  }

  function moveImage(fig, direction) {
    var sibling = direction === "up" ? fig.previousElementSibling : fig.nextElementSibling;
    if (!sibling) return;
    var fd = new FormData();
    fd.append("direction", direction);
    api(fig.dataset.url + "/move", { method: "POST", body: fd })
      .then(function () {
        if (direction === "up") fig.parentNode.insertBefore(fig, sibling);
        else fig.parentNode.insertBefore(sibling, fig);
      })
      .catch(function (err) { alert(err.message); });
  }

  document.querySelectorAll(".image-zone").forEach(function (zone) {
    var drop = zone.querySelector(".drop");
    if (!drop) return;
    var input = drop.querySelector('input[type="file"]');
    input.addEventListener("change", function () { upload(zone, input.files); input.value = ""; });
    drop.addEventListener("paste", function (e) {
      var files = [];
      Array.prototype.forEach.call(e.clipboardData.items || [], function (item) {
        if (item.kind === "file") files.push(item.getAsFile());
      });
      if (files.length) { e.preventDefault(); upload(zone, files); }
    });
    ["dragenter", "dragover"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("over"); });
    });
    drop.addEventListener("drop", function (e) { upload(zone, e.dataTransfer.files); });
  });

  // ---------- 截圖批次上傳：一次上傳一個檔案（不受單次上傳上限影響），保留資料夾路徑 ----------

  function walkEntry(entry, prefix, out) {
    return new Promise(function (resolve) {
      if (entry.isFile) {
        entry.file(function (f) { out.push({ file: f, path: prefix + f.name }); resolve(); }, function () { resolve(); });
      } else if (entry.isDirectory) {
        var reader = entry.createReader(), all = [];
        (function read() {
          reader.readEntries(function (ents) {
            if (!ents.length) {
              Promise.all(all.map(function (e) { return walkEntry(e, prefix + entry.name + "/", out); })).then(resolve);
            } else { all = all.concat(Array.prototype.slice.call(ents)); read(); }
          }, function () { resolve(); });
        })();
      } else { resolve(); }
    });
  }

  function batchUpload(zone, items) {
    if (!items.length) return;
    var bar = zone.querySelector("progress"), log = zone.querySelector(".batch-log");
    var ok = 0, skipped = [];
    bar.hidden = false; bar.max = items.length; bar.value = 0;
    zone.classList.add("busy");
    var chain = Promise.resolve();
    items.forEach(function (it) {
      chain = chain.then(function () {
        var fd = new FormData();
        fd.append("files", it.file, it.path);
        return api(zone.dataset.batchUpload, { method: "POST", body: fd }).then(function (data) {
          (data.results || []).forEach(function (r) {
            if (r.status === "staged") ok++; else skipped.push(r.path + "：" + r.note);
          });
        }).catch(function (err) { skipped.push(it.path + "：" + err.message); })
          .then(function () { bar.value += 1; log.textContent = "已上傳 " + bar.value + " / " + items.length; });
      });
    });
    chain.then(function () {
      zone.classList.remove("busy");
      if (skipped.length) alert("放入暫存區 " + ok + " 張，略過 " + skipped.length + " 個：\n" + skipped.slice(0, 30).join("\n"));
      location.reload();
    });
  }

  document.querySelectorAll("[data-batch-upload]").forEach(function (zone) {
    zone.querySelectorAll("[data-batch-files]").forEach(function (input) {
      input.addEventListener("change", function () {
        var items = Array.prototype.map.call(input.files, function (f) {
          return { file: f, path: f.webkitRelativePath || f.name };
        });
        input.value = "";
        batchUpload(zone, items);
      });
    });
    ["dragenter", "dragover"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove("over"); });
    });
    zone.addEventListener("drop", function (e) {
      var out = [], entries = Array.prototype.map.call(e.dataTransfer.items || [], function (i) {
        return i.webkitGetAsEntry ? i.webkitGetAsEntry() : null;
      }).filter(Boolean);
      if (!entries.length) {
        batchUpload(zone, Array.prototype.map.call(e.dataTransfer.files, function (f) { return { file: f, path: f.name }; }));
        return;
      }
      Promise.all(entries.map(function (en) { return walkEntry(en, "", out); })).then(function () {
        out.sort(function (a, b) { return a.path < b.path ? -1 : 1; });
        batchUpload(zone, out);
      });
    });
  });

  // ---------- 匯入歷史日報：逐檔送出，結果逐列顯示 ----------

  // 刪除選取的正式文件：全選、已選份數、送出前確認
  var delForm = document.querySelector("[data-del-official]");
  if (delForm) {
    var boxes = function () { return Array.prototype.slice.call(document.querySelectorAll("[data-sel]")); };
    var refresh = function () {
      var n = boxes().filter(function (b) { return b.checked; }).length;
      delForm.querySelector("[data-sel-count]").textContent = n;
      delForm.querySelector("[data-del-submit]").disabled = !n;
      delForm.classList.toggle("active", n > 0);
    };
    document.addEventListener("change", function (e) {
      if (e.target.matches("[data-sel-all]")) boxes().forEach(function (b) { b.checked = e.target.checked; });
      if (e.target.matches("[data-sel], [data-sel-all]")) refresh();
    });
    delForm.addEventListener("submit", function (e) {
      var picked = boxes().filter(function (b) { return b.checked; }).map(function (b) {
        var tr = b.closest("tr"); return tr ? tr.cells[1].textContent.trim() + " " + tr.cells[2].textContent.trim() : b.value;
      });
      if (!confirm("確定刪除這 " + picked.length + " 份正式文件？\n\n" + picked.slice(0, 20).join("\n") +
                   (picked.length > 20 ? "\n…等 " + picked.length + " 份" : "") + "\n\n刪除後文件編號會釋放，不能復原。")) e.preventDefault();
    });
    refresh();
  }

  // 匯入日報的正式 PDF：維護服務依序產生，這裡每 3 秒更新進度
  var pdfBox = document.querySelector("[data-pdf-status]"), pdfTimer = null;
  function fmtSec(s) { s = Math.max(0, Math.round(s)); return s >= 60 ? Math.floor(s / 60) + " 分 " + (s % 60) + " 秒" : s + " 秒"; }
  function pdfCell(td, it) {
    td.innerHTML = "";
    var el;
    if (it.state === "done") {
      el = document.createElement("a"); el.href = "/reports/" + it.report_id + "/final.pdf"; el.target = "_blank";
      el.className = "ok-text"; el.textContent = "✔ 已產生";
    } else if (it.state === "error") {
      el = document.createElement("a"); el.href = "/reports/" + it.report_id; el.className = "fail-text";
      el.textContent = "✘ 失敗"; el.title = it.message || "";
    } else if (it.state === "running") {
      el = document.createElement("span"); el.className = "warn"; el.textContent = "產生中…" + (it.elapsed != null ? "（" + it.elapsed + " 秒）" : "");
    } else {
      el = document.createElement("span"); el.className = "hint"; el.textContent = "排隊中" + (it.position ? "（第 " + it.position + " 位）" : "");
    }
    td.appendChild(el);
  }
  function pollPdf() {
    if (!pdfBox) return;
    clearTimeout(pdfTimer);
    api(pdfBox.dataset.pdfStatus).then(function (st) {
      var c = st.count, pending = c.queued + c.running;
      pdfBox.hidden = !st.total;
      var bar = pdfBox.querySelector("progress");
      bar.max = st.total || 1; bar.value = c.done;
      pdfBox.querySelector("[data-pdf-summary]").textContent = "已產生 " + c.done + " / " + st.total +
        (c.running ? "、產生中 " + c.running : "") + (c.queued ? "、排隊 " + c.queued : "") + (c.error ? "、失敗 " + c.error : "") +
        (pending ? "，預估還要約 " + fmtSec(st.eta_seconds) + "（每份約 " + st.seconds_per_pdf + " 秒）" : "，全部完成") +
        "（更新於 " + st.now + "）";
      var run = st.items.filter(function (i) { return i.state === "running"; })[0];
      pdfBox.querySelector("[data-pdf-current]").textContent = run
        ? "正在產生：" + run.date + "（" + run.doc_no + "）" + (run.elapsed != null ? "，已 " + run.elapsed + " 秒" : "")
        : (pending ? "等待維護服務處理…（若一直沒有開始，請確認 dailyreport-maint 服務正在執行）" : "");
      var ul = pdfBox.querySelector("[data-pdf-errors]"); ul.innerHTML = "";
      st.items.filter(function (i) { return i.state === "error"; }).forEach(function (i) {
        var li = document.createElement("li"), a = document.createElement("a");
        a.href = "/reports/" + i.report_id; a.textContent = i.date + "（" + i.doc_no + "）";
        li.className = "fail-text"; li.appendChild(a); li.appendChild(document.createTextNode("：" + (i.message || "失敗") + "（到日報頁按「重試」）"));
        ul.appendChild(li);
      });
      var byId = {};
      st.items.forEach(function (i) { byId[i.report_id] = i; });
      document.querySelectorAll("[data-pdf-cell]").forEach(function (td) {
        var it = byId[td.dataset.pdfCell];
        if (it) pdfCell(td, it);
      });
      pdfTimer = pending ? setTimeout(pollPdf, 3000) : null;
    }).catch(function () { pdfTimer = setTimeout(pollPdf, 10000); });
  }
  if (pdfBox && !pdfBox.hidden) pollPdf();

  document.querySelectorAll("[data-import-upload]").forEach(function (zone) {
    var table = document.querySelector("[data-import-results]"), tbody = table.tBodies[0];
    var bar = zone.querySelector("progress"), log = zone.querySelector(".import-log");
    var LABEL = { imported: "已匯入", would_import: "可以匯入", skipped: "略過", error: "拒絕" };
    function cell(tr, text, cls) {
      var td = document.createElement("td"); td.textContent = text == null ? "" : String(text);
      if (cls) td.className = cls; tr.appendChild(td); return td;
    }
    function addRow(r) {
      var tr = document.createElement("tr");
      cell(tr, r.file);
      cell(tr, LABEL[r.status] || r.status, (r.status === "error" ? "fail-text" : (r.status === "skipped" ? "warn" : "ok-text")) + " nowrap");
      var d = cell(tr, "");
      if (r.report_id) { var a = document.createElement("a"); a.href = "/reports/" + r.report_id; a.textContent = r.date; d.appendChild(a); }
      else d.textContent = r.date || "";
      cell(tr, r.doc_no); cell(tr, r.author); cell(tr, r.alarms);
      var c = r.counts || {}, parts = [];
      Object.keys(c).forEach(function (k) { parts.push(k + " " + c[k].matched + "/" + c[k].rows); });
      cell(tr, parts.join("、"));
      var pc = cell(tr, r.status === "imported" ? "排隊中" : "—", r.status === "imported" ? "hint nowrap" : "hint");
      if (r.status === "imported" && r.report_id) pc.dataset.pdfCell = r.report_id;
      var info = cell(tr, "");
      var msg = document.createElement("span"); msg.textContent = r.message || "";
      if (r.status === "error") msg.className = "fail-text";
      info.appendChild(msg);
      if (r.errors && r.errors.length) {  // 資料錯誤點（紅字）：人工修正後重新上傳複檢
        var eu = document.createElement("ul"); eu.className = "fail-text";
        r.errors.forEach(function (w) { var li = document.createElement("li"); li.textContent = w; eu.appendChild(li); });
        info.appendChild(eu);
      }
      if (r.warnings && r.warnings.length) {
        var ul = document.createElement("ul");
        r.warnings.forEach(function (w) { var li = document.createElement("li"); li.textContent = w; ul.appendChild(li); });
        info.appendChild(ul);
      }
      tbody.appendChild(tr);
      return tr;
    }
    var verdict = document.querySelector("[data-import-verdict]"), batch = null;
    function post(f, extra) {
      var fd = new FormData();
      fd.append("files", f, f.name);
      Object.keys(extra).forEach(function (k) { fd.append(k, extra[k]); });
      return api(zone.dataset.importUpload, { method: "POST", body: fd })
        .then(function (data) { return (data.results || [])[0] || { file: f.name, status: "error", message: "沒有回應" }; })
        .catch(function (err) { return { file: f.name, status: "error", message: err.message }; });
    }
    function setVerdict(text, cls, canImport) {
      verdict.hidden = false;
      verdict.className = "import-verdict top-gap " + cls;
      verdict.querySelector("[data-verdict-text]").textContent = text;
      verdict.querySelector("[data-import-confirm]").hidden = !canImport;
      verdict.querySelector("[data-import-cancel]").hidden = !canImport;
    }
    // 第一步：逐檔檢查（必做）。有任何格式不符 → 整批拒絕；重複的略過。
    function run(files) {
      files = files.filter(function (f) { return /\.xls[xm]$/i.test(f.name) && !/^~\$/.test(f.name); });
      if (!files.length) { alert("沒有 .xlsx / .xlsm 檔案"); return; }
      files.sort(function (x, y) { return (x.webkitRelativePath || x.name) < (y.webkitRelativePath || y.name) ? -1 : 1; });
      table.hidden = false; tbody.innerHTML = ""; verdict.hidden = true;
      bar.hidden = false; bar.max = files.length; bar.value = 0;
      var n = { would_import: 0, skipped: 0, error: 0 }, bySha = {}, byDate = {};
      batch = [];
      var chain = Promise.resolve();
      files.forEach(function (f) {
        chain = chain.then(function () {
          return post(f, { mode: "check" }).then(function (r) {
            if (r.status === "would_import") {  // 同一批裡的重複
              if (bySha[r.sha256]) { r.status = "skipped"; r.message = "與「" + bySha[r.sha256] + "」是同一個檔案，略過"; delete r.token; }
              else if (byDate[r.date]) { r.status = "skipped"; r.message = "與「" + byDate[r.date] + "」同一天（" + r.date + "），只匯入第一個，略過"; delete r.token; }
              else { bySha[r.sha256] = r.file; byDate[r.date] = r.file; }
            }
            n[r.status] = (n[r.status] || 0) + 1;
            var tr = addRow(r);
            if (r.status === "would_import") batch.push({ file: f, token: r.token, tr: tr });
            bar.value += 1;
            log.textContent = "檢查 " + bar.value + " / " + files.length + "：可以匯入 " + n.would_import + "、略過 " + n.skipped + "、拒絕 " + n.error;
          });
        });
      });
      chain.then(function () {
        var rej = n.error ? "拒絕 " + n.error + " 份（紅字列出錯誤點，這些檔案不會匯入；請人工檢查修正 Excel 後重新上傳複檢）" : "";
        var skip = n.skipped ? "略過 " + n.skipped + " 份（重複，原因寫在說明欄）" : "";
        if (!batch.length) {
          setVerdict("沒有可以匯入的檔案：" + [rej, skip].filter(Boolean).join("；") + "。", n.error ? "flash error" : "flash", false);
        } else {
          setVerdict("可以匯入 " + batch.length + " 份" + (rej || skip ? "；" + [rej, skip].filter(Boolean).join("；") : "") +
                     "。確認後按「確定匯入」只匯入檢查通過的檔案；匯入後為已結案日報，不能修改（檔案管理員以上可刪除後重匯）。",
                     n.error ? "flash warn-box" : "flash success", true);
          verdict.querySelector("[data-import-confirm]").textContent = "確定匯入 " + batch.length + " 份";
        }
      });
    }
    // 第二步：手動確定後才匯入（每個檔案都要帶檢查時的 token）
    verdict.querySelector("[data-import-confirm]").addEventListener("click", function () {
      var todo = batch || [];
      if (!todo.length) return;
      batch = null;
      verdict.querySelector("[data-import-confirm]").hidden = true;
      verdict.querySelector("[data-import-cancel]").hidden = true;
      bar.max = todo.length; bar.value = 0;
      var n = { imported: 0, skipped: 0, error: 0 }, chain = Promise.resolve();
      todo.forEach(function (it) {
        chain = chain.then(function () {
          return post(it.file, { mode: "import", token: it.token }).then(function (r) {
            n[r.status] = (n[r.status] || 0) + 1;
            var tr = addRow(r);
            tbody.replaceChild(tr, it.tr);
            bar.value += 1;
            log.textContent = "匯入 " + bar.value + " / " + todo.length + "：已匯入 " + n.imported + "、略過 " + n.skipped + "、錯誤 " + n.error;
            if (n.imported && !pdfTimer) pollPdf();  // 第一份匯入後就開始顯示 PDF 進度
          });
        });
      });
      chain.then(function () {
        setVerdict("匯入完成：已匯入 " + n.imported + " 份" + (n.skipped ? "、略過 " + n.skipped : "") + (n.error ? "、錯誤 " + n.error : "") +
                   "。正式 PDF 由維護服務依序產生，進度見上方。", n.error ? "flash error" : "flash success", false);
      });
    });
    verdict.querySelector("[data-import-cancel]").addEventListener("click", function () {
      batch = null; verdict.hidden = true; tbody.innerHTML = ""; table.hidden = true; bar.hidden = true; log.textContent = "已取消，沒有匯入任何檔案。";
    });
    zone.querySelectorAll("[data-import-files]").forEach(function (input) {
      input.addEventListener("change", function () { var fs = Array.prototype.slice.call(input.files); input.value = ""; run(fs); });
    });
    ["dragenter", "dragover"].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add("over"); }); });
    ["dragleave", "drop"].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove("over"); }); });
    zone.addEventListener("drop", function (e) {
      var out = [], entries = Array.prototype.map.call(e.dataTransfer.items || [], function (i) {
        return i.webkitGetAsEntry ? i.webkitGetAsEntry() : null;
      }).filter(Boolean);
      if (!entries.length) { run(Array.prototype.slice.call(e.dataTransfer.files)); return; }
      Promise.all(entries.map(function (en) { return walkEntry(en, "", out); })).then(function () {
        run(out.map(function (o) { return o.file; }));
      });
    });
  });

  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-use-suggest]");
    if (!b) return;
    var sel = document.querySelector('select[name="' + b.dataset.useSuggest + '"]');
    if (sel) { sel.value = b.dataset.value; markDirty(sel); }
  });
})();
