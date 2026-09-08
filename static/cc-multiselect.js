/* cc-multiselect.js — searchable combobox upgrade cho <select>.
   Cách dùng:
     <select name="shop_id" data-cc-search placeholder="-- Chọn shop --">...</select>
     <select name="shop_ids" multiple data-cc-search>...</select>
   Hỗ trợ <optgroup>. Form submit dùng select gốc (giữ name/value như cũ).
*/
(function () {
  "use strict";

  // Bỏ dấu để search không phân biệt dấu/ko dấu
  function strip(s) {
    return (s || "")
      .toString()
      .toLowerCase()
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .replace(/đ/g, "d");
  }

  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  function escapeHTML(s) {
    return (s == null ? "" : String(s))
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function CCMultiSelect(select) {
    var self = this;
    self.select = select;
    self.multiple = select.multiple;
    self.placeholder =
      select.getAttribute("placeholder") ||
      (self.multiple ? "-- Chọn --" : (select.querySelector("option[value='']") || {}).textContent || "-- Chọn --");

    // Build wrapper
    var wrap = el("div", "cc-ms" + (self.multiple ? " cc-ms-multi" : " cc-ms-single"));
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);

    var toggle = el("div", "cc-ms-toggle");
    toggle.setAttribute("tabindex", "0");
    wrap.appendChild(toggle);

    var panel = el("div", "cc-ms-panel");
    wrap.appendChild(panel);

    var searchWrap = el("div", "cc-ms-search-wrap");
    var search = el("input", "cc-ms-search");
    search.type = "search";
    search.placeholder = "🔎 Tìm…";
    searchWrap.appendChild(search);
    panel.appendChild(searchWrap);

    if (self.multiple) {
      var actions = el("div", "cc-ms-actions");
      var btnAll = el("button", null, "Chọn tất cả");
      btnAll.type = "button";
      var btnClear = el("button", null, "Bỏ chọn");
      btnClear.type = "button";
      actions.appendChild(btnAll);
      actions.appendChild(btnClear);
      panel.appendChild(actions);
      btnAll.addEventListener("click", function () {
        self.list
          .querySelectorAll(".cc-ms-opt:not(.cc-ms-hidden)")
          .forEach(function (o) { self.setSelected(o.dataset.value, true); });
        self.syncToggle();
      });
      btnClear.addEventListener("click", function () {
        Array.prototype.forEach.call(select.options, function (o) { o.selected = false; });
        self.refreshOpts();
        self.syncToggle();
      });
    }

    var list = el("div", "cc-ms-list");
    panel.appendChild(list);

    var countEl = el("div", "cc-ms-count");
    panel.appendChild(countEl);

    self.wrap = wrap;
    self.toggle = toggle;
    self.panel = panel;
    self.search = search;
    self.list = list;
    self.countEl = countEl;

    self.buildOptions();
    self.syncToggle();

    // Events
    toggle.addEventListener("click", function () { self.toggleOpen(); });
    toggle.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); self.toggleOpen(); }
    });
    search.addEventListener("input", function () { self.applyFilter(); });
    search.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { self.close(); }
      if (e.key === "Enter") {
        e.preventDefault();
        var first = list.querySelector(".cc-ms-opt:not(.cc-ms-hidden)");
        if (first) self.toggleOption(first.dataset.value);
      }
    });

    document.addEventListener("click", function (e) {
      if (!wrap.contains(e.target)) self.close();
    });
  }

  CCMultiSelect.prototype.buildOptions = function () {
    var self = this;
    self.list.innerHTML = "";
    var children = self.select.children;

    function addOption(opt, groupLabel) {
      if (opt.value === "" && !self.multiple && opt.textContent.trim().startsWith("--")) {
        // Bỏ placeholder dạng "-- Chọn --" trong list (đã có ở toggle)
        return;
      }
      // Bỏ qua option đang ẩn (caller set opt.hidden để lọc động — vd NV theo team)
      if (opt.hidden) return;
      var row = el("div", "cc-ms-opt");
      row.dataset.value = opt.value;
      row.dataset.search = strip(opt.textContent + " " + opt.value);
      var html = "";
      if (self.multiple) {
        html += '<input type="checkbox"' + (opt.selected ? " checked" : "") + " />";
      }
      html += '<span class="cc-ms-opt-label">' + escapeHTML(opt.textContent) + "</span>";
      row.innerHTML = html;
      if (opt.selected) row.classList.add("selected");
      row.addEventListener("click", function (e) {
        if (e.target.tagName === "INPUT") return; // checkbox already toggles
        self.toggleOption(opt.value);
      });
      if (self.multiple) {
        row.querySelector("input").addEventListener("change", function () {
          self.toggleOption(opt.value);
        });
      }
      self.list.appendChild(row);
    }

    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      if (c.tagName === "OPTGROUP") {
        var label = el("div", "cc-ms-opt-group", escapeHTML(c.label || ""));
        self.list.appendChild(label);
        for (var j = 0; j < c.children.length; j++) addOption(c.children[j], c.label);
      } else if (c.tagName === "OPTION") {
        addOption(c, null);
      }
    }
    self.updateCount();
  };

  CCMultiSelect.prototype.toggleOpen = function () {
    if (this.wrap.classList.contains("open")) this.close();
    else this.open();
  };
  CCMultiSelect.prototype.open = function () {
    // Close other open multiselects
    document.querySelectorAll(".cc-ms.open").forEach(function (w) { w.classList.remove("open"); });
    this.wrap.classList.add("open");
    this.search.value = "";
    this.applyFilter();
    this.position();
    // Đóng khi cuộn/resize để panel (position:fixed) không bị "trôi" lệch khỏi nút.
    if (!this._onScroll) {
      var self = this;
      this._onScroll = function (e) {
        // Cuộn BÊN TRONG panel (danh sách lựa chọn) → KHÔNG đóng.
        // Chỉ đóng khi cuộn trang/khung ngoài (lúc đó panel position:fixed sẽ trôi lệch).
        if (e && e.type === "scroll" && e.target && e.target.nodeType === 1
            && self.panel && self.panel.contains(e.target)) return;
        self.close();
      };
      window.addEventListener("scroll", this._onScroll, true);
      window.addEventListener("resize", this._onScroll, true);
    }
    setTimeout(() => this.search.focus(), 10);
  };
  // Neo panel bằng position:fixed theo nút → thoát mọi khung overflow (table-scroll...).
  CCMultiSelect.prototype.position = function () {
    var r = this.toggle.getBoundingClientRect();
    var p = this.panel;
    p.style.position = "fixed";
    p.style.left = r.left + "px";
    p.style.right = "auto";
    p.style.width = Math.max(r.width, 240) + "px";
    // Lật lên trên nếu không đủ chỗ phía dưới
    var spaceBelow = window.innerHeight - r.bottom;
    var ph = p.offsetHeight || 260;
    if (spaceBelow < ph + 8 && r.top > spaceBelow) {
      p.style.top = "auto";
      p.style.bottom = (window.innerHeight - r.top + 4) + "px";
    } else {
      p.style.bottom = "auto";
      p.style.top = (r.bottom + 4) + "px";
    }
  };
  CCMultiSelect.prototype.close = function () {
    this.wrap.classList.remove("open");
    if (this._onScroll) {
      window.removeEventListener("scroll", this._onScroll, true);
      window.removeEventListener("resize", this._onScroll, true);
      this._onScroll = null;
    }
  };

  CCMultiSelect.prototype.applyFilter = function () {
    var q = strip(this.search.value);
    var visible = 0;
    this.list.querySelectorAll(".cc-ms-opt").forEach(function (o) {
      var match = !q || o.dataset.search.indexOf(q) >= 0;
      o.classList.toggle("cc-ms-hidden", !match);
      o.style.display = match ? "" : "none";
      if (match) visible++;
    });
    // ẩn group label nếu không có option visible bên dưới
    this.list.querySelectorAll(".cc-ms-opt-group").forEach(function (g) {
      var next = g.nextElementSibling;
      var any = false;
      while (next && next.classList.contains("cc-ms-opt")) {
        if (!next.classList.contains("cc-ms-hidden")) { any = true; break; }
        next = next.nextElementSibling;
      }
      g.style.display = any ? "" : "none";
    });
    if (visible === 0) {
      if (!this.list.querySelector(".cc-ms-empty")) {
        this.list.appendChild(el("div", "cc-ms-empty", "Không tìm thấy"));
      }
    } else {
      var em = this.list.querySelector(".cc-ms-empty");
      if (em) em.remove();
    }
  };

  CCMultiSelect.prototype.setSelected = function (value, on) {
    Array.prototype.forEach.call(this.select.options, function (o) {
      if (o.value === value) o.selected = on;
    });
    this.refreshOpts();
  };

  CCMultiSelect.prototype.toggleOption = function (value) {
    var opt = null;
    Array.prototype.forEach.call(this.select.options, function (o) { if (o.value === value) opt = o; });
    if (!opt) return;
    if (this.multiple) {
      opt.selected = !opt.selected;
    } else {
      Array.prototype.forEach.call(this.select.options, function (o) { o.selected = false; });
      opt.selected = true;
      this.close();
    }
    this.select.dispatchEvent(new Event("change", { bubbles: true }));
    this.refreshOpts();
    this.syncToggle();
  };

  CCMultiSelect.prototype.refreshOpts = function () {
    var self = this;
    self.list.querySelectorAll(".cc-ms-opt").forEach(function (row) {
      var opt = null;
      Array.prototype.forEach.call(self.select.options, function (o) {
        if (o.value === row.dataset.value) opt = o;
      });
      if (!opt) return;
      var cb = row.querySelector("input[type=checkbox]");
      if (cb) cb.checked = opt.selected;
      row.classList.toggle("selected", opt.selected);
    });
    self.updateCount();
  };

  CCMultiSelect.prototype.updateCount = function () {
    var total = this.list.querySelectorAll(".cc-ms-opt").length;
    var sel = 0;
    Array.prototype.forEach.call(this.select.options, function (o) { if (o.selected && o.value !== "") sel++; });
    this.countEl.textContent = (this.multiple ? sel + " / " : "") + total + " mục";
  };

  CCMultiSelect.prototype.clearAll = function () {
    Array.prototype.forEach.call(this.select.options, function (o) { o.selected = false; });
    this.select.dispatchEvent(new Event("change", { bubbles: true }));
    this.refreshOpts();
    this.syncToggle();
  };

  CCMultiSelect.prototype.syncToggle = function () {
    var self = this;
    self.toggle.innerHTML = "";
    var selectedOpts = [];
    Array.prototype.forEach.call(self.select.options, function (o) {
      if (o.selected && o.value !== "") selectedOpts.push(o);
    });

    var addCaret = function () {
      // Nút xoá (chỉ hiện khi có giá trị, không hiện khi select required và chưa cho phép bỏ)
      if (selectedOpts.length > 0) {
        var clr = el("span", "cc-ms-clear");
        clr.title = "Xoá lựa chọn";
        clr.textContent = "×";
        clr.addEventListener("click", function (e) {
          e.stopPropagation();
          self.clearAll();
        });
        self.toggle.appendChild(clr);
      }
      self.toggle.appendChild(el("span", "cc-ms-caret"));
    };

    if (selectedOpts.length === 0) {
      self.toggle.appendChild(el("span", "cc-ms-placeholder", escapeHTML(self.placeholder)));
      addCaret();
      return;
    }
    if (!self.multiple) {
      self.toggle.appendChild(el("span", null, escapeHTML(selectedOpts[0].textContent)));
      addCaret();
      return;
    }
    var max = 3;
    selectedOpts.slice(0, max).forEach(function (o) {
      var chip = el("span", "cc-ms-chip");
      chip.innerHTML =
        '<span class="cc-ms-chip-label">' + escapeHTML(o.textContent) + "</span>" +
        '<span class="cc-ms-chip-x" title="Bỏ chọn">×</span>';
      chip.querySelector(".cc-ms-chip-x").addEventListener("click", function (e) {
        e.stopPropagation();
        self.setSelected(o.value, false);
        self.select.dispatchEvent(new Event("change", { bubbles: true }));
        self.syncToggle();
      });
      self.toggle.appendChild(chip);
    });
    if (selectedOpts.length > max) {
      self.toggle.appendChild(el("span", "cc-ms-more", "+" + (selectedOpts.length - max)));
    }
    addCaret();
  };

  function upgrade(sel) {
    if (sel.dataset.ccInit) return;
    sel.dataset.ccInit = "1";
    sel._ccms = new CCMultiSelect(sel);
  }

  function initAll(root) {
    (root || document).querySelectorAll("select[data-cc-search]").forEach(upgrade);
  }

  // Rebuild options sau khi caller đổi <select>.innerHTML (vd AJAX load shop list).
  function refresh(sel) {
    if (!sel) return;
    if (sel._ccms && typeof sel._ccms.buildOptions === "function") {
      sel._ccms.buildOptions();
      sel._ccms.syncToggle();
      sel._ccms.updateCount();
    }
  }

  // Public API
  window.CCMultiSelect = {
    init: initAll,
    upgrade: upgrade,
    refresh: refresh,
  };

  // Auto-init
  function bootstrap() {
    initAll();
    // Theo dõi DOM — tự upgrade các <select data-cc-search> được render sau (JS template, AJAX...)
    try {
      var mo = new MutationObserver(function (muts) {
        muts.forEach(function (m) {
          m.addedNodes && m.addedNodes.forEach(function (n) {
            if (n.nodeType !== 1) return;
            if (n.matches && n.matches("select[data-cc-search]")) upgrade(n);
            if (n.querySelectorAll) n.querySelectorAll("select[data-cc-search]").forEach(upgrade);
          });
        });
      });
      mo.observe(document.body, { childList: true, subtree: true });
    } catch (e) { /* IE/edge — ignore */ }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
