/**
 * Zalo Bridge — listen Zalo group messages, POST sang Flask backend.
 *
 * Auth: cookie + IMEI + userAgent của tài khoản "Trợ lý Lan" (1 account riêng).
 *   Lấy bằng extension ZaloDataExtractor (xem README zca-js).
 *
 * Config: .env trong cùng folder.
 *   ZALO_COOKIES   = JSON string (mảng cookie từ extension)
 *   ZALO_IMEI      = imei từ extension
 *   ZALO_USER_AGENT= UA browser
 *   FLASK_URL      = http://localhost:5050/api/zalo-bridge/inbound
 *   BRIDGE_SECRET  = secret shared với Flask
 *   ALLOWED_GROUPS = (optional) CSV list group thread_id. Rỗng = nghe tất cả.
 */
require("dotenv").config();
const { Zalo, ThreadType, FriendEventType, LoginQRCallbackEventType } = require("zca-js");
const http = require("http");
const fs = require("fs");
const path = require("path");

// ── QR login state (cho UI đăng nhập lại Lan qua QR) ──
const ENV_PATH = path.join(__dirname, ".env");
let qrState = { status: "idle", image: "", display_name: "", error: "", ts: 0 };

function _setEnvLine(text, key, value) {
  const line = `${key}=${value}`;
  const re = new RegExp("^" + key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "=.*$", "m");
  return re.test(text) ? text.replace(re, line) : (text.replace(/\n$/, "") + (text ? "\n" : "") + line + "\n");
}

function saveCredentialsToEnv(cookieArr, imei, userAgent) {
  let cur = "";
  try { cur = fs.readFileSync(ENV_PATH, "utf8"); } catch (e) {}
  const cookieJson = JSON.stringify(cookieArr);
  let next = cur;
  next = _setEnvLine(next, "ZALO_COOKIES", `'${cookieJson}'`);
  next = _setEnvLine(next, "ZALO_IMEI", imei);
  next = _setEnvLine(next, "ZALO_USER_AGENT", `'${userAgent}'`);
  const tmp = ENV_PATH + ".tmp";
  fs.writeFileSync(tmp, next, { mode: 0o600 });
  fs.renameSync(tmp, ENV_PATH);
}

const FLASK_URL = process.env.FLASK_URL || "http://localhost:5050/api/zalo-bridge/inbound";
const BRIDGE_SECRET = process.env.BRIDGE_SECRET || "";
const ALLOWED_GROUPS = (process.env.ALLOWED_GROUPS || "")
  .split(",").map(s => s.trim()).filter(Boolean);
const OUTBOUND_PORT = parseInt(process.env.OUTBOUND_PORT || "5051", 10);
// Bỏ qua tin > N phút (catch-up sau restart / tin cũ delivered lại)
const MAX_AGE_MIN = parseInt(process.env.MAX_AGE_MIN || "5", 10);
// Track thời điểm bridge start — tin trước mốc này = lịch sử, skip
const BRIDGE_START_TS = Date.now();

// ── Server QR-only: chạy khi CHƯA có cookie (đăng nhập lần đầu) ──
// Cookie hết → bridge full vẫn phục vụ QR; nhưng lần ĐẦU chưa có cookie thì
// không được exit, phải mở server QR để quét lấy cookie (tránh chicken-egg).
function startQrOnlyServer() {
  const server = http.createServer((req, res) => {
    if ((req.headers["x-bridge-secret"] || "") !== BRIDGE_SECRET) {
      res.writeHead(401); return res.end("unauthorized");
    }
    if (req.method === "POST" && req.url === "/login-qr/start") {
      qrState = { status: "starting", image: "", display_name: "", error: "", ts: Date.now() };
      const zaloQR = new Zalo();
      zaloQR.loginQR({ userAgent: process.env.ZALO_USER_AGENT || "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36", language: "vi" }, (ev) => {
        try {
          if (ev.type === LoginQRCallbackEventType.QRCodeGenerated) {
            const img = ev.data.image || "";
            qrState = { status: "waiting", image: img.startsWith("data:") ? img : ("data:image/png;base64," + img), display_name: "", error: "", ts: Date.now() };
            console.log("🔲 QR generated (QR-only), chờ quét...");
          } else if (ev.type === LoginQRCallbackEventType.QRCodeScanned) {
            qrState.status = "scanned"; qrState.display_name = (ev.data && ev.data.display_name) || "";
            console.log(`📷 QR đã quét bởi ${qrState.display_name}`);
          } else if (ev.type === LoginQRCallbackEventType.QRCodeExpired) {
            qrState.status = "expired"; qrState.ts = Date.now();
          } else if (ev.type === LoginQRCallbackEventType.QRCodeDeclined) {
            qrState.status = "declined"; qrState.ts = Date.now();
          } else if (ev.type === LoginQRCallbackEventType.GotLoginInfo) {
            try {
              saveCredentialsToEnv(ev.data.cookie, ev.data.imei, ev.data.userAgent);
              qrState.status = "success"; qrState.ts = Date.now();
              console.log("✅ QR login OK (lần đầu) — đã lưu .env. Chờ Flask restart bridge.");
            } catch (e) { qrState.status = "error"; qrState.error = "save env: " + e.message; }
          }
        } catch (e) { console.warn("QR callback error:", e.message); }
      }).catch((e) => { qrState.status = "error"; qrState.error = e.message; console.warn("loginQR error:", e.message); });
      res.writeHead(202, {"Content-Type":"application/json"});
      return res.end(JSON.stringify({ok:true, started:true}));
    }
    if (req.method === "GET" && req.url === "/login-qr/status") {
      res.writeHead(200, {"Content-Type":"application/json"});
      return res.end(JSON.stringify({ ok:true, ...qrState }));
    }
    res.writeHead(503, {"Content-Type":"application/json"});
    res.end(JSON.stringify({ok:false, error:"bridge chưa đăng nhập (QR-only mode) — quét QR để bắt đầu"}));
  });
  server.listen(OUTBOUND_PORT, "127.0.0.1", () => {
    console.log(`📡 QR-only HTTP on 127.0.0.1:${OUTBOUND_PORT} — chờ quét QR (chưa có cookie)`);
  });
}

const HAS_COOKIES = !!(process.env.ZALO_COOKIES && process.env.ZALO_IMEI && process.env.ZALO_USER_AGENT);

let cookies = null;
if (!HAS_COOKIES) {
  console.warn("⚠ Chưa có cookie Zalo — chạy chế độ QR-only để đăng nhập lần đầu.");
  startQrOnlyServer();
} else {
  try {
    cookies = JSON.parse(process.env.ZALO_COOKIES);
  } catch (e) {
    console.error("❌ ZALO_COOKIES không phải JSON hợp lệ:", e.message);
    process.exit(1);
  }
}

// Dùng module http/https (KHÔNG dùng fetch) vì Node fetch chặn "bad ports"
// theo chuẩn WHATWG — port 5060 (SIP) bị từ chối → "bad port". Flask cpqc chạy
// 5060 nên phải gửi qua http.request để không bị chặn.
function postToFlask(payload) {
  return new Promise((resolve) => {
    try {
      const u = new URL(FLASK_URL);
      const lib = u.protocol === "https:" ? require("https") : require("http");
      const body = JSON.stringify(payload);
      const req = lib.request({
        hostname: u.hostname,
        port: u.port || (u.protocol === "https:" ? 443 : 80),
        path: u.pathname + u.search,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
          "X-Bridge-Secret": BRIDGE_SECRET,
        },
      }, (res) => {
        let text = "";
        res.on("data", (c) => (text += c));
        res.on("end", () => { console.log(`[POST] ${res.statusCode} ${text.slice(0, 200)}`); resolve(); });
      });
      req.on("error", (e) => { console.warn(`[POST] error: ${e.message}`); resolve(); });
      req.setTimeout(20000, () => { req.destroy(new Error("timeout")); });
      req.write(body);
      req.end();
    } catch (e) {
      console.warn(`[POST] error: ${e.message}`);
      resolve();
    }
  });
}

if (HAS_COOKIES) (async () => {
  console.log("🧸 Zalo Bridge khởi động...");
  // imageMetadataGetter: cần để zca-js upload ảnh từ file path
  // (đo width/height/size bằng image-size).
  const { imageSize: sizeOf } = require("image-size");
  const zalo = new Zalo({
    imageMetadataGetter: async (filePath) => {
      try {
        const buf = fs.readFileSync(filePath);
        const dim = sizeOf(buf);
        return {
          width: dim.width || 0,
          height: dim.height || 0,
          size: buf.length,
        };
      } catch (e) {
        console.warn("imageMetadataGetter fail:", e.message);
        return null;
      }
    },
  });
  let api;
  try {
    api = await zalo.login({
      cookie: cookies,
      imei: process.env.ZALO_IMEI,
      userAgent: process.env.ZALO_USER_AGENT,
      language: "vi",
    });
  } catch (e) {
    console.error("❌ Login Zalo fail:", e.message);
    console.error("Stack:", e.stack);
    if (e.cause) console.error("Cause:", e.cause);
    // Cookie hết hạn/không dùng được → KHÔNG exit (systemd sẽ restart loop và
    // trang /lan-qr không gọi được bridge). Mở server QR-only để quét lại.
    console.warn("↻ Chuyển sang chế độ QR-only — vào moon.tieuhiem.com/lan-qr để quét lại.");
    try { startQrOnlyServer(); } catch (e2) { console.error("QR-only fail:", e2.message); }
    return;   // dừng luồng full, giữ tiến trình sống phục vụ QR
  }
  const ctx = api.getContext();
  console.log(`✅ Đã đăng nhập. uid=${ctx.uid}`);

  api.listener.on("message", async (message) => {
    try {
      // Bỏ qua tin do chính bot gửi
      if (message.isSelf) return;
      // Cho phép cả Group + User 1-1. Phân biệt khi POST sang Flask.
      const isUserThread = message.type === ThreadType.User;
      const isGroupThread = message.type === ThreadType.Group;
      if (!isUserThread && !isGroupThread) return;
      // Detect text vs image
      const content = message.data?.content;
      const msgType = message.data?.msgType || "";
      const isText = typeof content === "string" && content.trim().length > 0;
      const isImage = (msgType === "chat.photo" || msgType === "photo") &&
                      typeof content === "object" && (content?.href || content?.thumbUrl);
      if (!isText && !isImage) return;
      // Filter group nếu có allowlist
      const tid = String(message.threadId);
      if (ALLOWED_GROUPS.length > 0 && !ALLOWED_GROUPS.includes(tid)) return;

      // Filter tuổi tin: bỏ qua tin > MAX_AGE_MIN phút HOẶC tin tồn tại
      // trước khi bridge start (catch-up / lịch sử)
      const msgTs = Number(message.data?.ts || 0);
      if (msgTs > 0) {
        const ageMs = Date.now() - msgTs;
        if (ageMs > MAX_AGE_MIN * 60 * 1000) {
          console.log(`⏭ skip old msg (${Math.round(ageMs/60000)} phút) [${tid}]`);
          return;
        }
        if (msgTs < BRIDGE_START_TS - 60_000) {
          // Tin có ts trước lúc bridge start hơn 1 phút → là catch-up cũ
          console.log(`⏭ skip pre-start msg [${tid}]`);
          return;
        }
      }

      const senderId = String(message.data?.uidFrom || "");
      const senderName = message.data?.dName || "";

      const threadType = isUserThread ? "user" : "group";
      const prefix = isUserThread ? "🔒1-1" : "";

      // 1-1: kiểm tra kết bạn. Nếu chưa thấy trong cache → refresh ngay
      // (NV mới kết bạn vài giây trước, cache 10p chưa update kịp).
      if (isUserThread && senderId && !_friendUids.has(senderId)) {
        console.log(`🔄 1-1 ${senderId} chưa trong cache, refresh friends...`);
        await refreshFriends();
        if (!_friendUids.has(senderId)) {
          console.log(`⚠ 1-1 non-friend xác nhận ${senderId} (${senderName})`);
          await replyAddFriend(tid);
          return;
        }
        console.log(`✓ 1-1 ${senderId} đã kết bạn (sau refresh) — xử lý tiếp`);
      }

      if (isImage) {
        const url = content.href || content.thumbUrl;
        const caption = content.title || content.description || "";
        console.log(`🖼 ${prefix}[${tid}] ${senderName}: <ảnh> ${caption || url.slice(0, 60)}`);
        await postToFlask({
          kind: "image",
          thread_type: threadType,
          zalo_thread_id: tid,
          zalo_msg_id: String(message.data?.msgId || ""),
          zalo_sender_id: senderId,
          zalo_sender_name: senderName,
          image_url: url,
          caption,
          sent_at: message.data?.ts ? new Date(Number(message.data.ts)).toISOString() : new Date().toISOString(),
        });
        return;
      }

      console.log(`📩 ${prefix}[${tid}] ${senderName} (${senderId}): ${content.slice(0, 80)}`);

      await postToFlask({
        thread_type: threadType,
        zalo_thread_id: tid,
        zalo_msg_id: String(message.data?.msgId || ""),
        zalo_sender_id: senderId,
        zalo_sender_name: senderName,
        body: content,
        sent_at: message.data?.ts ? new Date(Number(message.data.ts)).toISOString() : new Date().toISOString(),
      });
    } catch (e) {
      console.warn("⚠ on(message) error:", e.message);
    }
  });

  api.listener.onConnected(() => console.log("🔌 Listener connected"));
  api.listener.onClosed((reason) => {
    console.warn("🔌 Listener closed:", reason);
    process.exit(3);  // systemd Restart=always sẽ tự bật lại
  });
  api.listener.onError((err) => console.warn("⚠ Listener error:", err?.message || err));

  // ── Auto-accept friend requests ──
  api.listener.on("friend_event", async (evt) => {
    try {
      if (evt.type !== FriendEventType.REQUEST) return;
      // evt.data thường có {fromUid, fromName, msg, ...}
      const fromUid = String(evt.data?.fromUid || evt.threadId || "");
      const fromName = evt.data?.fromDisplayName || evt.data?.fromName || "bạn";
      if (!fromUid) return;
      console.log(`📨 Friend request từ ${fromName} (${fromUid}) — auto-accept`);
      try {
        await api.acceptFriendRequest(fromUid);
        console.log(`✓ Accepted: ${fromUid}`);
      } catch (e) {
        console.warn(`accept failed: ${e.message}`);
        return;
      }
      // Refresh cache + gửi welcome
      await refreshFriends();
      try {
        const welcome = `🌸 Chào bạn ${fromName}! Lan đã đồng ý kết bạn rồi nha.\n\n` +
                        `Từ giờ bạn có thể gửi chi phí riêng cho Lan qua chat này — tin nhắn hoặc ảnh hoá đơn đều được ạ 🧸\n\n` +
                        `Vd:\n  • "Chi VPP 200k"\n  • "Thanh toán đo đạc 600k"\n  • 📷 ảnh SMS bank / hoá đơn\n\n` +
                        `Lan sẽ ghi vô sổ kế toán + báo lại cho anh chị admin ngay 🌷`;
        await api.sendMessage({ msg: welcome }, fromUid, ThreadType.User);
      } catch (e) {
        console.warn(`welcome msg fail: ${e.message}`);
      }
    } catch (exc) {
      console.warn("friend_event handler error:", exc.message);
    }
  });

  api.listener.start();

  // ── Cache friend list để biết user 1-1 đã kết bạn chưa ──
  let _friendUids = new Set();
  async function refreshFriends() {
    try {
      const data = await api.getAllFriends();
      const list = Array.isArray(data) ? data : (data?.friends || data?.items || []);
      const newSet = new Set();
      for (const f of list) {
        const uid = String(f.userId || f.uid || f.id || "");
        if (uid) newSet.add(uid);
      }
      _friendUids = newSet;
      console.log(`👥 Friends loaded: ${_friendUids.size}`);
    } catch (e) {
      console.warn("refreshFriends error:", e.message);
    }
  }
  setTimeout(refreshFriends, 5000);
  setInterval(refreshFriends, 5 * 60 * 1000);  // refresh mỗi 5 phút

  // Helper: reply hint kết bạn cho non-friend 1-1
  async function replyAddFriend(toUid) {
    try {
      const msg = "🌸 Chào bạn! Lan là Trợ lý ghi nhận chi phí của Tiểu Hiềm.\n\n" +
                 "⚠ Bạn LƯU Ý hãy gửi LỜI MỜI KẾT BẠN với Lan trước nha — phải là bạn bè thì Lan mới có thể ghi nhận chi phí riêng cho bạn ạ.\n\n" +
                 "Sau khi đã kết bạn, bạn gửi lại tin/ảnh chi phí cho Lan, Lan sẽ ghi vô sổ kế toán ngay 🧸";
      await api.sendMessage({ msg }, toUid, ThreadType.User);
      console.log(`👋 reply add-friend hint → ${toUid}`);
    } catch (e) {
      console.warn("replyAddFriend error:", e.message);
    }
  }

  // ── Catchup: quét tin gần đây để bù tin miss khi bridge restart / nghỉ ──
  async function scanRecent(hours = 6) {
    try {
      const all = await api.getAllGroups();
      const gids = Object.keys(all.gridVerMap || all || {});
      const cutoff = Date.now() - hours * 60 * 60 * 1000;
      let scanned = 0, forwarded = 0;
      for (const gid of gids) {
        if (ALLOWED_GROUPS.length > 0 && !ALLOWED_GROUPS.includes(gid)) continue;
        try {
          // Số tin mỗi group: scan ngắn (≤6h) lấy 30, scan dài (≥24h) lấy 200 để không sót
          const limit = hours >= 24 ? 200 : 30;
          const hist = await api.getGroupChatHistory(gid, limit);
          // hist = { groupMsgs: [GroupMessage, ...] }
          const msgs = hist?.groupMsgs || hist?.messages || (Array.isArray(hist) ? hist : []);
          for (const m of msgs) {
            const ts = Number(m.ts || m.data?.ts || 0);
            if (!ts || ts < cutoff) continue;
            if (m.isSelf || (m.data && m.data.uidFrom === ctx.uid)) continue;
            const content = m.content ?? m.data?.content;
            const msgType = m.msgType || m.data?.msgType || "";
            const senderId = String(m.uidFrom || m.data?.uidFrom || "");
            const senderName = m.dName || m.data?.dName || "";
            const msgId = String(m.msgId || m.data?.msgId || "");
            const isText = typeof content === "string" && content.trim();
            const isImage = (msgType === "chat.photo" || msgType === "photo") &&
                            typeof content === "object" && (content?.href || content?.thumbUrl);
            scanned++;
            if (isText) {
              await postToFlask({
                zalo_thread_id: gid, zalo_msg_id: msgId,
                zalo_sender_id: senderId, zalo_sender_name: senderName,
                body: content,
                sent_at: new Date(ts).toISOString(),
              });
              forwarded++;
            } else if (isImage) {
              await postToFlask({
                kind: "image",
                zalo_thread_id: gid, zalo_msg_id: msgId,
                zalo_sender_id: senderId, zalo_sender_name: senderName,
                image_url: content.href || content.thumbUrl,
                caption: content.title || content.description || "",
                sent_at: new Date(ts).toISOString(),
              });
              forwarded++;
            }
          }
        } catch (e) {
          console.warn(`scan ${gid} error:`, e.message);
        }
      }
      console.log(`🔍 Catchup ${hours}h: scanned=${scanned}, forwarded=${forwarded}`);
    } catch (e) {
      console.warn("scanRecent error:", e.message);
    }
  }

  // Chạy ngay sau khi listener connect (delay 8s để listener ổn định)
  setTimeout(() => scanRecent(6), 8000);
  // Định kỳ mỗi 2 giờ scan 4h gần nhất (chồng lấp + dedup ở Flask)
  setInterval(() => scanRecent(4), 2 * 60 * 60 * 1000);

  // ── HTTP server nội bộ để Flask đẩy tin Lan về Zalo group ──
  const server = http.createServer(async (req, res) => {
    if ((req.headers["x-bridge-secret"] || "") !== BRIDGE_SECRET) {
      res.writeHead(401); return res.end("unauthorized");
    }
    // GET /list-groups — trả list group Lan đang trong
    if (req.method === "GET" && req.url === "/list-groups") {
      try {
        const all = await api.getAllGroups();
        // all = { gridVerMap: {gid: ver, ...} } — chỉ là list gid
        const gids = Object.keys(all.gridVerMap || all || {});
        if (gids.length === 0) {
          res.writeHead(200, {"Content-Type":"application/json"});
          return res.end(JSON.stringify({groups: []}));
        }
        // Lấy info chi tiết để có tên
        const info = await api.getGroupInfo(gids);
        // info.gridInfoMap = {gid: {name, memberIds, ...}}
        const groups = [];
        const infoMap = info.gridInfoMap || info || {};
        for (const gid of gids) {
          const g = infoMap[gid] || {};
          groups.push({
            thread_id: gid,
            name: g.name || g.fullName || `Group ${gid.slice(0, 10)}`,
            member_count: (g.memberIds || g.totalMember || []).length || g.totalMember || 0,
          });
        }
        res.writeHead(200, {"Content-Type":"application/json"});
        return res.end(JSON.stringify({groups}));
      } catch (e) {
        console.warn("list-groups error:", e.message);
        res.writeHead(500); return res.end(JSON.stringify({error: e.message}));
      }
    }
    // GET /people[?thread_id=...] — danh sách người + uid để UI chọn theo TÊN
    // Không có thread_id → bạn bè của Lan. Có thread_id → thành viên nhóm đó.
    if (req.method === "GET" && req.url.startsWith("/people")) {
      try {
        const q = new URL(req.url, "http://x");
        const gid = q.searchParams.get("thread_id") || "";
        let people = [];
        if (gid) {
          let uids = [];
          try {
            const gm = await api.getGroupMembers(String(gid));
            uids = (gm?.memberIds || gm?.members || gm || []).map(m =>
              String(m?.userId || m?.uid || m?.id || m));
          } catch (e) {
            const info = await api.getGroupInfo([String(gid)]);
            const g = (info.gridInfoMap || {})[String(gid)] || {};
            uids = (g.memberIds || []).map(String);
          }
          uids = uids.filter(Boolean).slice(0, 200);
          for (let i = 0; i < uids.length; i += 20) {
            const chunk = uids.slice(i, i + 20);
            try {
              const inf = await api.getUserInfo(chunk);
              const map = inf?.changed_profiles || inf?.profiles || inf || {};
              for (const uid of chunk) {
                const p = map[uid] || map[String(uid)] || {};
                people.push({ uid: String(uid), name: p.displayName || p.zaloName || p.username || "" });
              }
            } catch (e) {
              for (const uid of chunk) people.push({ uid: String(uid), name: "" });
            }
          }
        } else {
          const data = await api.getAllFriends();
          const list = Array.isArray(data) ? data : (data?.friends || data?.items || []);
          people = list.map(f => ({
            uid: String(f.userId || f.uid || f.id || ""),
            name: f.displayName || f.zaloName || f.username || "",
          })).filter(p => p.uid);
        }
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ people }));
      } catch (e) {
        console.warn("people error:", e.message);
        res.writeHead(500); return res.end(JSON.stringify({ error: e.message }));
      }
    }
    // POST /rescan?hours=168 — quét lại tin cũ N giờ, gửi về Flask (dedup qua zalo_msg_id)
    if (req.method === "POST" && req.url.startsWith("/rescan")) {
      const url = new URL(req.url, "http://x");
      const hours = Math.max(1, Math.min(720, parseInt(url.searchParams.get("hours") || "168", 10)));
      // chạy bất đồng bộ — không chặn response
      scanRecent(hours).catch(e => console.warn("rescan error:", e.message));
      res.writeHead(202, {"Content-Type":"application/json"});
      return res.end(JSON.stringify({ok:true, started:true, hours}));
    }
    // POST /leave-group?thread_id=... — bot Lan tự rời 1 nhóm
    if (req.method === "POST" && req.url.startsWith("/leave-group")) {
      const url = new URL(req.url, "http://x");
      const gid = url.searchParams.get("thread_id") || "";
      if (!gid) { res.writeHead(400); return res.end(JSON.stringify({ok:false, error:"missing thread_id"})); }
      api.leaveGroup(String(gid), false)
        .then(function(){
          console.log(`🚪 Đã rời nhóm ${gid}`);
          res.writeHead(200, {"Content-Type":"application/json"});
          res.end(JSON.stringify({ok:true, left:gid}));
        })
        .catch(function(e){
          console.warn("leave-group error:", e.message);
          res.writeHead(500); res.end(JSON.stringify({ok:false, error:e.message}));
        });
      return;
    }
    // POST /login-qr/start — sinh QR đăng nhập lại Lan (cookie hết hạn/đổi mk)
    if (req.method === "POST" && req.url === "/login-qr/start") {
      qrState = { status: "starting", image: "", display_name: "", error: "", ts: Date.now() };
      const zaloQR = new Zalo();
      zaloQR.loginQR({ userAgent: process.env.ZALO_USER_AGENT, language: "vi" }, (ev) => {
        try {
          if (ev.type === LoginQRCallbackEventType.QRCodeGenerated) {
            // image là base64 PNG → prefix data URI cho <img>
            const img = ev.data.image || "";
            qrState = { status: "waiting", image: img.startsWith("data:") ? img : ("data:image/png;base64," + img),
                        display_name: "", error: "", ts: Date.now() };
            console.log("🔲 QR generated, chờ quét...");
          } else if (ev.type === LoginQRCallbackEventType.QRCodeScanned) {
            qrState.status = "scanned";
            qrState.display_name = (ev.data && ev.data.display_name) || "";
            console.log(`📷 QR đã quét bởi ${qrState.display_name}`);
          } else if (ev.type === LoginQRCallbackEventType.QRCodeExpired) {
            qrState.status = "expired"; qrState.ts = Date.now();
            console.log("⌛ QR hết hạn");
          } else if (ev.type === LoginQRCallbackEventType.QRCodeDeclined) {
            qrState.status = "declined"; qrState.ts = Date.now();
            console.log("🚫 QR bị từ chối");
          } else if (ev.type === LoginQRCallbackEventType.GotLoginInfo) {
            try {
              saveCredentialsToEnv(ev.data.cookie, ev.data.imei, ev.data.userAgent);
              qrState.status = "success"; qrState.ts = Date.now();
              console.log("✅ QR login OK — đã lưu .env. Chờ Flask restart bridge.");
            } catch (e) {
              qrState.status = "error"; qrState.error = "save env: " + e.message;
              console.warn("QR save env error:", e.message);
            }
          }
        } catch (e) { console.warn("QR callback error:", e.message); }
      }).catch((e) => {
        qrState.status = "error"; qrState.error = e.message;
        console.warn("loginQR error:", e.message);
      });
      res.writeHead(202, {"Content-Type":"application/json"});
      return res.end(JSON.stringify({ok:true, started:true}));
    }
    // GET /login-qr/status — frontend poll
    if (req.method === "GET" && req.url === "/login-qr/status") {
      res.writeHead(200, {"Content-Type":"application/json"});
      return res.end(JSON.stringify({ ok:true, ...qrState }));
    }
    // POST /send
    if (req.method !== "POST" || req.url !== "/send") {
      res.writeHead(404); return res.end("not found");
    }
    let body = "";
    req.on("data", chunk => body += chunk);
    req.on("end", async () => {
      let tmpFile = "";
      try {
        const { thread_id, text, mentions, thread_type, image_url } = JSON.parse(body || "{}");
        if (!thread_id || (!text && !image_url)) {
          res.writeHead(400); return res.end(JSON.stringify({ok:false, error:"missing thread_id and text/image_url"}));
        }
        const sendPayload = { msg: String(text || "") };
        if (Array.isArray(mentions) && mentions.length > 0) {
          sendPayload.mentions = mentions;
        }

        // Nếu có image_url: download → save tạm → attach
        if (image_url) {
          try {
            const https = require("https");
            const http = require("http");
            const os = require("os");
            const crypto = require("crypto");
            const u = new URL(String(image_url));
            const client = u.protocol === "https:" ? https : http;
            const buf = await new Promise((resolve, reject) => {
              const timer = setTimeout(() => reject(new Error("download timeout")), 15000);
              client.get(image_url, (r) => {
                if (r.statusCode !== 200) {
                  clearTimeout(timer);
                  return reject(new Error("HTTP " + r.statusCode));
                }
                const chunks = [];
                r.on("data", (c) => chunks.push(c));
                r.on("end", () => { clearTimeout(timer); resolve(Buffer.concat(chunks)); });
                r.on("error", reject);
              }).on("error", reject);
            });
            // Đoán đuôi từ URL
            const m = (u.pathname.match(/\.(jpg|jpeg|png|webp|gif)$/i) || [null, "jpg"]);
            const ext = m[1].toLowerCase();
            tmpFile = path.join(os.tmpdir(), `bridge-img-${crypto.randomBytes(6).toString("hex")}.${ext}`);
            fs.writeFileSync(tmpFile, buf);
            sendPayload.attachments = tmpFile;
          } catch (eImg) {
            console.warn("📤 image download fail:", eImg.message);
            // Vẫn gửi text nếu có
            if (!text) {
              res.writeHead(500); return res.end(JSON.stringify({ok:false, error:"image download fail: " + eImg.message}));
            }
          }
        }

        const sendType = thread_type === "user" ? ThreadType.User : ThreadType.Group;
        await api.sendMessage(sendPayload, String(thread_id), sendType);
        console.log(`📤 [${thread_id}] ${image_url ? "🖼+" : ""}${String(text || "").slice(0, 80)}`);
        res.writeHead(200, {"Content-Type":"application/json"});
        res.end(JSON.stringify({ok:true}));
      } catch (e) {
        console.warn("📤 send error:", e.message);
        res.writeHead(500); res.end(JSON.stringify({ok:false, error:e.message}));
      } finally {
        if (tmpFile) {
          setTimeout(() => { try { fs.unlinkSync(tmpFile); } catch(_) {} }, 10000);
        }
      }
    });
  });
  server.listen(OUTBOUND_PORT, "127.0.0.1", () => {
    console.log(`📡 Outbound HTTP listening on 127.0.0.1:${OUTBOUND_PORT}/send`);
  });
})();
