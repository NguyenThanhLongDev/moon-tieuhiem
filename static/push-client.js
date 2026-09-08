/* Web Push client — đăng ký subscription với backend + UI toggle */
(function () {
  'use strict';

  function b64UrlToUint8Array(b64) {
    const padding = '='.repeat((4 - b64.length % 4) % 4);
    const base64 = (b64 + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  async function ensureSwReady() {
    if (!('serviceWorker' in navigator)) throw new Error('Browser không hỗ trợ Service Worker');
    let reg = await navigator.serviceWorker.getRegistration('/');
    if (!reg) reg = await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    return reg;
  }

  async function currentSubscription() {
    const reg = await ensureSwReady();
    return await reg.pushManager.getSubscription();
  }

  async function subscribe() {
    if (!('PushManager' in window)) throw new Error('Browser không hỗ trợ Push API');
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') throw new Error('Bạn chưa cho phép thông báo');

    const reg = await ensureSwReady();
    // Lấy VAPID key từ server
    const keyRes = await fetch('/api/push/vapid-key');
    const keyData = await keyRes.json();
    if (!keyData.ok) throw new Error('Không lấy được VAPID key: ' + keyData.error);

    // Có thể đã tồn tại subscription → dùng lại
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: b64UrlToUint8Array(keyData.publicKey),
      });
    }

    // Gửi lên server
    const res = await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ subscription: sub.toJSON() }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'Lưu subscription thất bại');
    return sub;
  }

  async function unsubscribe() {
    const sub = await currentSubscription();
    if (!sub) return;
    await fetch('/api/push/unsubscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ endpoint: sub.endpoint }),
    }).catch(() => {});
    await sub.unsubscribe().catch(() => {});
  }

  async function sendTest() {
    const res = await fetch('/api/push/test', {
      method: 'POST',
      credentials: 'same-origin',
    });
    return await res.json();
  }

  // Expose API
  window.TieuHiemPush = {
    subscribe,
    unsubscribe,
    currentSubscription,
    sendTest,
    async isEnabled() {
      try {
        if (!('Notification' in window)) return false;
        if (Notification.permission !== 'granted') return false;
        const sub = await currentSubscription();
        return !!sub;
      } catch (e) { return false; }
    },
  };

  /* ── Auto-sync subscription lên server mỗi lần tải trang ─────────
     Lý do: trên iOS PWA, nếu lần subscribe trước fetch bị lỗi hoặc
     subscription bị server xoá (failure_count cao) mà client vẫn giữ
     local sub → user thấy nút "đã bật" nhưng DB không có → push im lặng.
     Idempotent: DB dùng ON CONFLICT (endpoint) DO UPDATE. */
  async function syncLocalSubscriptionToServer() {
    try {
      if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
      if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
      const reg = await navigator.serviceWorker.getRegistration('/');
      if (!reg) return;
      const sub = await reg.pushManager.getSubscription();
      if (!sub) return;
      await fetch('/api/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ subscription: sub.toJSON() }),
      }).catch(() => {});
    } catch (e) { /* silent */ }
  }

  /* ── UI helper: auto-wire buttons có data-push-action ────────────── */
  document.addEventListener('DOMContentLoaded', async () => {
    // Sync sẵn subscription lên server (chạy ngầm, không chặn UI)
    syncLocalSubscriptionToServer();

    const btns = document.querySelectorAll('[data-push-action]');
    if (!btns.length) return;

    async function refreshButtons() {
      const enabled = await window.TieuHiemPush.isEnabled();
      btns.forEach(btn => {
        const act = btn.dataset.pushAction;
        if (act === 'toggle') {
          btn.dataset.pushEnabled = enabled ? '1' : '0';
          const label = btn.querySelector('[data-push-label]') || btn;
          label.textContent = enabled ? 'Đã bật thông báo' : 'Bật thông báo';
          // Trạng thái thể hiện qua class .is-on (CSS lo phần màu) — không ép inline style nữa
          btn.classList.toggle('is-on', enabled);
          const icon = btn.querySelector('.bi');
          if (icon) { icon.classList.toggle('bi-bell-fill', enabled); icon.classList.toggle('bi-bell', !enabled); }
        }
      });
    }

    btns.forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.preventDefault();
        const act = btn.dataset.pushAction;
        btn.disabled = true;
        try {
          if (act === 'toggle') {
            const enabled = await window.TieuHiemPush.isEnabled();
            if (enabled) {
              await window.TieuHiemPush.unsubscribe();
              alert('Đã tắt thông báo.');
            } else {
              await window.TieuHiemPush.subscribe();
              alert('Đã bật thông báo! Thử gửi thông báo test để kiểm tra.');
            }
            await refreshButtons();
          } else if (act === 'test') {
            const r = await window.TieuHiemPush.sendTest();
            if (r.ok) alert('Đã gửi test — sent=' + r.sent + ', failed=' + r.failed);
            else alert('Lỗi: ' + r.error);
          }
        } catch (err) {
          alert('Lỗi: ' + err.message);
        } finally {
          btn.disabled = false;
        }
      });
    });

    await refreshButtons();
  });
})();
