/* ─── Searchable Select — common component ─────────────────────────────────
 * Vanilla JS, no jQuery. Auto-init các <select data-searchable="true">.
 *
 * Cách dùng:
 *   <select data-searchable="true" name="uid" onchange="...">
 *     <option value="1">Anh</option>
 *     <option value="2">Bình</option>
 *   </select>
 *
 * Hỗ trợ:
 *  - Click trigger để mở dropdown
 *  - Gõ search → highlight chữ trùng + filter list
 *  - ↑/↓ chọn item, Enter xác nhận, Esc đóng
 *  - Click ngoài → tự đóng
 *  - Đồng bộ giá trị + onchange với <select> gốc → form/handler cũ vẫn chạy
 *
 * API JS:
 *   ccSelectInit(selectEl)            // init thủ công
 *   ccSelectInitAll(rootEl?)          // quét init tất cả trong rootEl (default: document)
 */
(function() {
  'use strict';

  function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }
  function escapeRegex(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }
  function highlightMatch(text, query) {
    if (!query) return escapeHTML(text);
    const re = new RegExp('(' + escapeRegex(query) + ')', 'ig');
    return escapeHTML(text).replace(re, '<mark>$1</mark>');
  }

  function ccSelectInit(selectEl) {
    if (!selectEl || selectEl.dataset.ccSsInit === '1') return;
    selectEl.dataset.ccSsInit = '1';

    const wrap = document.createElement('div');
    wrap.className = 'cc-ss';
    selectEl.classList.add('cc-ss-native');
    selectEl.parentNode.insertBefore(wrap, selectEl);
    wrap.appendChild(selectEl);

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'cc-ss-trigger';

    const popover = document.createElement('div');
    popover.className = 'cc-ss-popover';
    popover.hidden = true;
    popover.innerHTML =
      '<div class="cc-ss-search-wrap"><input type="text" class="cc-ss-search" placeholder="Tìm..." autocomplete="off"></div>' +
      '<div class="cc-ss-list"></div>' +
      '<div class="cc-ss-empty" hidden>Không có kết quả</div>';

    wrap.appendChild(trigger);
    wrap.appendChild(popover);

    const searchInput = popover.querySelector('.cc-ss-search');
    const listEl = popover.querySelector('.cc-ss-list');
    const emptyEl = popover.querySelector('.cc-ss-empty');

    function syncTrigger() {
      const opt = selectEl.options[selectEl.selectedIndex];
      const txt = opt ? opt.textContent.trim() : '';
      const placeholder = selectEl.dataset.placeholder || 'Chọn...';
      trigger.textContent = txt || placeholder;
      trigger.classList.toggle('is-empty', !txt);
    }

    function renderList(filter) {
      filter = (filter || '').trim();
      const f = filter.toLowerCase();
      listEl.innerHTML = '';
      let visible = 0;
      const frag = document.createDocumentFragment();
      for (let i = 0; i < selectEl.options.length; i++) {
        const opt = selectEl.options[i];
        const txt = opt.textContent;
        if (f && txt.toLowerCase().indexOf(f) === -1) continue;
        const item = document.createElement('div');
        item.className = 'cc-ss-item' + (opt.value === selectEl.value ? ' is-selected' : '');
        item.dataset.value = opt.value;
        item.dataset.idx = i;
        item.innerHTML = highlightMatch(txt, filter);
        frag.appendChild(item);
        visible++;
      }
      listEl.appendChild(frag);
      emptyEl.hidden = visible > 0;
      // Highlight current selected as active for keyboard nav baseline
      const items = listEl.querySelectorAll('.cc-ss-item');
      const activeIdx = Array.from(items).findIndex(it => it.classList.contains('is-selected'));
      if (activeIdx >= 0) items[activeIdx].classList.add('is-active');
      else if (items.length) items[0].classList.add('is-active');
    }

    function open() {
      if (!popover.hidden) return;
      popover.hidden = false;
      searchInput.value = '';
      renderList('');
      setTimeout(() => searchInput.focus(), 0);
    }
    function close() { popover.hidden = true; }
    function toggle() { popover.hidden ? open() : close(); }

    function pickValue(value) {
      if (selectEl.value === value) { close(); return; }
      selectEl.value = value;
      selectEl.dispatchEvent(new Event('change', { bubbles: true }));
      syncTrigger();
      close();
    }

    function moveActive(delta) {
      const items = Array.from(listEl.querySelectorAll('.cc-ss-item'));
      if (!items.length) return;
      let cur = items.findIndex(it => it.classList.contains('is-active'));
      if (cur < 0) cur = 0;
      else items[cur].classList.remove('is-active');
      cur = (cur + delta + items.length) % items.length;
      items[cur].classList.add('is-active');
      items[cur].scrollIntoView({ block: 'nearest' });
    }

    trigger.addEventListener('click', toggle);
    trigger.addEventListener('keydown', function(ev) {
      if (ev.key === 'Enter' || ev.key === ' ' || ev.key === 'ArrowDown') {
        ev.preventDefault();
        open();
      }
    });

    searchInput.addEventListener('input', e => renderList(e.target.value));
    searchInput.addEventListener('keydown', function(ev) {
      if (ev.key === 'ArrowDown') { ev.preventDefault(); moveActive(+1); }
      else if (ev.key === 'ArrowUp')   { ev.preventDefault(); moveActive(-1); }
      else if (ev.key === 'Enter') {
        ev.preventDefault();
        const active = listEl.querySelector('.cc-ss-item.is-active');
        if (active) pickValue(active.dataset.value);
      }
      else if (ev.key === 'Escape') { ev.preventDefault(); close(); trigger.focus(); }
    });

    listEl.addEventListener('click', function(ev) {
      const item = ev.target.closest('.cc-ss-item');
      if (item) pickValue(item.dataset.value);
    });
    listEl.addEventListener('mouseover', function(ev) {
      const item = ev.target.closest('.cc-ss-item');
      if (!item) return;
      listEl.querySelectorAll('.cc-ss-item.is-active').forEach(el => el.classList.remove('is-active'));
      item.classList.add('is-active');
    });

    document.addEventListener('click', function(ev) {
      if (!wrap.contains(ev.target)) close();
    });

    selectEl.addEventListener('change', syncTrigger);
    syncTrigger();
  }

  function ccSelectInitAll(rootEl) {
    rootEl = rootEl || document;
    rootEl.querySelectorAll('select[data-searchable="true"]').forEach(ccSelectInit);
  }

  // Expose API + auto-init on DOMReady
  window.ccSelectInit    = ccSelectInit;
  window.ccSelectInitAll = ccSelectInitAll;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => ccSelectInitAll());
  } else {
    ccSelectInitAll();
  }
})();
