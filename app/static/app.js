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

  function cloneTemplate(tpl, container) {
    var next = parseInt(container.dataset.next || "0", 10);
    container.dataset.next = next + 1;
    var html = tpl.innerHTML.replace(/__i__/g, String(next));
    container.insertAdjacentHTML("beforeend", html);
    markDirty(container);
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
      t.closest("tr").remove();
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
    var form = el.closest && el.closest("form[data-dirty-guard]");
    if (form) dirtyForms.add(form);
  }
  document.addEventListener("input", function (e) { markDirty(e.target); });
  document.addEventListener("change", function (e) { markDirty(e.target); });
  document.addEventListener("submit", function (e) { dirtyForms.delete(e.target); dirtyForms.clear(); });
  window.addEventListener("beforeunload", function (e) {
    if (dirtyForms.size) { e.preventDefault(); e.returnValue = ""; }
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

  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-use-suggest]");
    if (!b) return;
    var sel = document.querySelector('select[name="' + b.dataset.useSuggest + '"]');
    if (sel) { sel.value = b.dataset.value; markDirty(sel); }
  });
})();
