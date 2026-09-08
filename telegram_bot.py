import json
import subprocess
from stock_telegram_helper import handle_stock_command
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from telegram_access import restriction_enabled, verify_group_interactive_access

import os
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PROJECT_DIR = "/home/admin/posbot"


async def gate_interactive_access(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    True nếu được xử lý lệnh (whitelist group + getChatMember).
    Khi từ chối: trả DENY_MESSAGE (tin nhắn hoặc callback + reply).
    """
    ok, deny_msg = await verify_group_interactive_access(context.bot, update)
    if ok:
        return True
    if update.message:
        await update.message.reply_text(deny_msg)
    elif update.callback_query and update.callback_query.message:
        q = update.callback_query
        await q.answer()
        await q.message.reply_text(deny_msg)
    return False


def register_user_for_broadcast(update: Update) -> None:
    """Khi chưa bật whitelist nhóm: ghi chat riêng để nhận broadcast (hành vi cũ)."""
    if restriction_enabled():
        return
    chat = update.effective_chat
    if not chat or chat.type != ChatType.PRIVATE:
        return
    try:
        from telegram_notify import register_private_chat

        register_private_chat(chat.id)
    except Exception:
        pass


def split_message(text, max_length=4000):
    parts = []
    while len(text) > max_length:
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = max_length
        parts.append(text[:split_at])
        text = text[split_at:].lstrip()
    if text:
        parts.append(text)
    return parts


async def send_long_message(update: Update, text: str):
    if not text or not text.strip():
        text = "Khong co du lieu phu hop."
    for part in split_message(text):
        await update.message.reply_text(part)


async def send_text_to_message(message, text: str):
    if not text or not text.strip():
        text = "Khong co du lieu phu hop."
    for part in split_message(text):
        await message.reply_text(part)


def run_script(cmd):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=PROJECT_DIR
    )
    if result.returncode != 0:
        return result.stderr.strip() or result.stdout.strip() or "Khong co du lieu phu hop."
    return result.stdout.strip() or result.stderr.strip() or "Khong co du lieu phu hop."


def normalize_text(text: str) -> str:
    return text.strip().lower()


def detect_intent(text: str) -> str:
    t = normalize_text(text)

    # Nhánh mới: NHAN DINH riêng, không đè PHAN TICH cũ
    if "nhan dinh" in t or "tom tat nhanh" in t or "insight" in t:
        return "insight"

    if (
        "kiem tra qc" in t
        or "kiem tra quang cao" in t
        or "kiem tra chi phi quang cao" in t
        or "shop nao chua nhap ads" in t
    ):
        return "ads"

    if "xem don hang" in t or "kiem tra don" in t or "don hang" in t:
        return "orders"

    if "xem tong hop" in t or "tong hop" in t:
        return "summary"

    # Giữ nguyên PHAN TICH cũ
    if (
        "xem phan tich" in t
        or "phan tich" in t
        or "shop nao" in t
        or "cao nhat" in t
        or "thap nhat" in t
        or " lo " in f" {t} "
    ):
        return "analysis"

    if "top" in t:
        return "top"

    if "so sanh" in t:
        return "compare"

    if (
        "xem doanh thu" in t
        or "doanh thu" in t
        or "loi nhuan" in t
        or " lai " in f" {t} "
        or t.startswith("lai ")
    ):
        return "report"

    return "report"


def build_command_from_intent(intent: str, text: str):
    if intent == "insight":
        return ["python3", "insight_engine.py", text]

    if intent == "ads":
        return ["python3", "ads_report.py", text]

    if intent == "orders":
        return ["python3", "report_orders.py", text]

    if intent == "summary":
        return ["python3", "ceo_report_engine.py", text]

    if intent == "analysis":
        return ["python3", "analysis_engine.py", text]

    if intent == "top":
        return ["python3", "top_engine.py", text]

    if intent == "compare":
        return ["python3", "compare_engine.py", text]

    return ["python3", "report_engine.py", text]


def main_menu_markup():
    keyboard = [
        [InlineKeyboardButton("📊 DOANH THU", callback_data="menu_revenue")],
        [InlineKeyboardButton("📦 DON HANG", callback_data="menu_orders")],
        [InlineKeyboardButton("📈 TONG HOP", callback_data="menu_summary")],
        [InlineKeyboardButton("🚨 ADS", callback_data="menu_ads")],
        [InlineKeyboardButton("🧠 PHAN TICH", callback_data="menu_analysis")],
        [InlineKeyboardButton("💡 NHAN DINH", callback_data="menu_insight")],
        [InlineKeyboardButton("⚙️ LENH NHANH", callback_data="menu_commands")],
    ]
    return InlineKeyboardMarkup(keyboard)


def back_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Quay lai menu", callback_data="menu_main")]]
    )


def submenu_markup(kind):
    keyboard = [
        [InlineKeyboardButton("Hom nay", callback_data=f"run_{kind}_today")],
        [InlineKeyboardButton("Hom qua", callback_data=f"run_{kind}_yesterday")],
        [InlineKeyboardButton("Hom kia", callback_data=f"run_{kind}_daybefore")],
        [InlineKeyboardButton("Xem theo ngay", callback_data=f"hint_{kind}_date")],
        [InlineKeyboardButton("⬅️ Quay lai menu", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_interactive_access(update, context):
        return
    register_user_for_broadcast(update)
    text = "Bot da san sang.\n\nChi can nho 1 lenh: /huongdan"
    await update.message.reply_text(text, reply_markup=main_menu_markup())


async def huongdan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_interactive_access(update, context):
        return
    register_user_for_broadcast(update)
    text = (
        "HUONG DAN SU DUNG BOT\n\n"
        "Chi can nho 1 lenh: /huongdan\n"
        "Bam vao tung nhom de xem nhanh.\n\n"
        "Neu go tay, dung form don gian:\n"
        "- xem doanh thu hom nay\n"
        "- xem don hang hom qua\n"
        "- xem tong hop 10/03/2026\n"
        "- phan tich hom kia\n"
        "- nhan dinh hom qua\n"
        "- so sanh hom qua\n"
        "- kiem tra qc"
    )
    await update.message.reply_text(text, reply_markup=main_menu_markup())


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_interactive_access(update, context):
        return
    register_user_for_broadcast(update)
    await update.message.reply_text("MENU NHANH", reply_markup=main_menu_markup())


def _load_shops_from_db() -> list:
    """Đọc danh sách shops từ DB PostgreSQL."""
    import sys, os
    sys.path.insert(0, "/home/admin1/tieuhiemsoft/posbottieuhiem")
    try:
        from db import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT s.shop_key, s.shop_name, s.pancake_shop_id, s.status::text,
                       w.pos_api_key
                FROM shops s
                LEFT JOIN wh_shops w ON w.shop_key = s.shop_key
                ORDER BY s.shop_key
            """)
            return [
                {"shop_key": r[0], "shop_name": r[1], "shop_id": r[2] or "",
                 "status": r[3], "pos_api_key": r[4] or ""}
                for r in cur.fetchall() if r[0]
            ]
    except Exception:
        pass
    # Fallback JSON
    try:
        with open(f"{PROJECT_DIR}/shops.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


async def shops_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_interactive_access(update, context):
        return
    register_user_for_broadcast(update)
    try:
        shops = _load_shops_from_db()
        if not shops:
            reply = "Chua co shop nao."
        else:
            lines = ["Danh sach shop dang theo doi:"]
            for i, shop in enumerate(shops, start=1):
                has_key = "✓" if shop.get("pos_api_key") else "✗"
                lines.append(
                    f"{i}. {shop.get('shop_name', '')} ({shop.get('shop_key', '')} | {shop.get('shop_id', '')}) | {shop.get('status', '')} | API:{has_key}"
                )
            reply = "\n".join(lines)
    except Exception as e:
        reply = f"Loi doc shops: {e}"

    await send_long_message(update, reply)


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_interactive_access(update, context):
        return
    register_user_for_broadcast(update)
    await update.message.reply_text("Dang chay sync POS, cho mot chut...")
    await send_long_message(update, run_script(["python3", "sync_pos.py"]))


async def addshop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_interactive_access(update, context):
        return
    register_user_for_broadcast(update)
    try:
        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Cu phap: /addshop ten_shop shop_id")
            return

        shop_name = args[0]
        shop_id = args[1]

        import sys
        sys.path.insert(0, "/home/admin1/tieuhiemsoft/posbottieuhiem")
        from db import get_conn

        with get_conn() as conn:
            cur = conn.cursor()
            # Tạo shop_key tự động: shopN (N = max số hiện tại + 1)
            cur.execute("""
                SELECT COALESCE(MAX(CAST(SUBSTRING(shop_key FROM 5) AS INTEGER)), 0)
                FROM shops
                WHERE shop_key ~ '^shop[0-9]+$'
            """)
            max_num = cur.fetchone()[0]
            shop_key = f"shop{max_num + 1}"

            # Kiểm tra shop_id đã tồn tại chưa
            cur.execute("SELECT shop_key FROM shops WHERE pancake_shop_id = %s", (shop_id,))
            existing = cur.fetchone()
            if existing:
                reply = f"Shop ID {shop_id} da ton tai voi key: {existing[0]}"
                await send_long_message(update, reply)
                return

            # INSERT vào shops
            cur.execute("""
                INSERT INTO shops (shop_key, shop_name, pancake_shop_id, status)
                VALUES (%s, %s, %s, 'active'::record_status)
                ON CONFLICT (shop_key) DO NOTHING
            """, (shop_key, shop_name, shop_id))

            # INSERT vào wh_shops
            cur.execute("""
                INSERT INTO wh_shops (shop_key, shop_name, pancake_shop_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (shop_key) DO UPDATE
                    SET shop_name = EXCLUDED.shop_name,
                        pancake_shop_id = EXCLUDED.pancake_shop_id
            """, (shop_key, shop_name, shop_id))

            conn.commit()

        reply = f"Da them shop thanh cong:\n- Ten: {shop_name}\n- ID: {shop_id}\n- Key: {shop_key}"
    except Exception as e:
        reply = f"Loi khi them shop: {e}"

    await send_long_message(update, reply)


async def renameshop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_interactive_access(update, context):
        return
    register_user_for_broadcast(update)
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Cu phap: /renameshop shop_key ten_moi")
            return

        shop_key = args[0]
        new_name = " ".join(args[1:])

        import sys
        sys.path.insert(0, "/home/admin1/tieuhiemsoft/posbottieuhiem")
        from db import get_conn

        with get_conn() as conn:
            cur = conn.cursor()

            # Kiểm tra shop_key tồn tại không
            cur.execute("SELECT shop_name FROM shops WHERE shop_key = %s", (shop_key,))
            row = cur.fetchone()
            if not row:
                await update.message.reply_text(f"Khong tim thay shop_key: {shop_key}")
                return

            old_name = row[0]

            # UPDATE shops
            cur.execute(
                "UPDATE shops SET shop_name = %s WHERE shop_key = %s",
                (new_name, shop_key)
            )
            # UPDATE wh_shops
            cur.execute(
                "UPDATE wh_shops SET shop_name = %s WHERE shop_key = %s",
                (new_name, shop_key)
            )
            conn.commit()

        reply = f"Da doi ten shop:\n- Key: {shop_key}\n- Cu: {old_name}\n- Moi: {new_name}"
    except Exception as e:
        reply = f"Loi: {e}"

    await send_long_message(update, reply)


async def kiemtradon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_interactive_access(update, context):
        return
    register_user_for_broadcast(update)
    args = " ".join(context.args).strip() if context.args else "hom nay"
    reply = run_script(["python3", "report_orders.py", args])
    await send_long_message(update, reply)


async def kiemtraqc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_interactive_access(update, context):
        return
    register_user_for_broadcast(update)
    args = " ".join(context.args).strip() if context.args else "kiem tra qc"
    reply = run_script(["python3", "ads_report.py", args])
    await send_long_message(update, reply)


async def menu_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await gate_interactive_access(update, context):
        return
    await query.answer()
    register_user_for_broadcast(update)
    data = query.data

    if data == "menu_main":
        await query.edit_message_text("MENU HUONG DAN", reply_markup=main_menu_markup())
        return

    if data == "menu_revenue":
        await query.edit_message_text("DOANH THU", reply_markup=submenu_markup("revenue"))
        return

    if data == "menu_orders":
        await query.edit_message_text("DON HANG", reply_markup=submenu_markup("orders"))
        return

    if data == "menu_summary":
        await query.edit_message_text("TONG HOP", reply_markup=submenu_markup("summary"))
        return

    if data == "menu_analysis":
        await query.edit_message_text(
            "PHAN TICH",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Hom nay", callback_data="run_analysis_today")],
                [InlineKeyboardButton("Hom qua", callback_data="run_analysis_yesterday")],
                [InlineKeyboardButton("Hom kia", callback_data="run_analysis_daybefore")],
                [InlineKeyboardButton("Top hom qua", callback_data="run_top_yesterday")],
                [InlineKeyboardButton("So sanh hom qua", callback_data="run_compare_yesterday")],
                [InlineKeyboardButton("Shop lo hom qua", callback_data="run_loss_yesterday")],
                [InlineKeyboardButton("⬅️ Quay lai menu", callback_data="menu_main")],
            ])
        )
        return

    if data == "menu_insight":
        await query.edit_message_text("NHAN DINH", reply_markup=submenu_markup("insight"))
        return

    if data == "menu_ads":
        await query.edit_message_text(
            "ADS",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Kiem tra QC", callback_data="run_ads_check")],
                [InlineKeyboardButton("⬅️ Quay lai menu", callback_data="menu_main")],
            ])
        )
        return

    if data == "menu_commands":
        await query.edit_message_text(
            text=(
                "LENH NHANH\n\n"
                "/start\n"
                "/huongdan\n"
                "/help\n"
                "/menu\n"
                "/shops\n"
                "/sync\n"
                "/addshop ten_shop shop_id\n"
                "/renameshop shop_key ten_moi\n"
                "/kiemtradon\n"
                "/kiemtraqc"
            ),
            reply_markup=back_markup()
        )
        return

    hints = {
        "hint_revenue_date": "Nhap theo dang: xem doanh thu 10/03/2026",
        "hint_orders_date": "Nhap theo dang: xem don hang 10/03/2026",
        "hint_summary_date": "Nhap theo dang: xem tong hop 10/03/2026",
        "hint_insight_date": "Nhap theo dang: nhan dinh 10/03/2026",
    }
    if data in hints:
        await query.message.reply_text(hints[data])
        return

    run_map = {
        "run_revenue_today": ["python3", "report_engine.py", "bao cao doanh thu hom nay"],
        "run_revenue_yesterday": ["python3", "report_engine.py", "bao cao doanh thu hom qua"],
        "run_revenue_daybefore": ["python3", "report_engine.py", "bao cao doanh thu hom kia"],

        "run_orders_today": ["python3", "report_orders.py", "hom nay"],
        "run_orders_yesterday": ["python3", "report_orders.py", "hom qua"],
        "run_orders_daybefore": ["python3", "report_orders.py", "hom kia"],

        "run_summary_today": ["python3", "ceo_report_engine.py", "hom nay"],
        "run_summary_yesterday": ["python3", "ceo_report_engine.py", "hom qua"],
        "run_summary_daybefore": ["python3", "ceo_report_engine.py", "hom kia"],

        "run_analysis_today": ["python3", "analysis_engine.py", "phan tich hom nay"],
        "run_analysis_yesterday": ["python3", "analysis_engine.py", "phan tich hom qua"],
        "run_analysis_daybefore": ["python3", "analysis_engine.py", "phan tich hom kia"],

        "run_insight_today": ["python3", "insight_engine.py", "hom nay"],
        "run_insight_yesterday": ["python3", "insight_engine.py", "hom qua"],
        "run_insight_daybefore": ["python3", "insight_engine.py", "hom kia"],

        "run_top_yesterday": ["python3", "top_engine.py", "top hom qua"],
        "run_compare_yesterday": ["python3", "compare_engine.py", "so sanh hom qua"],
        "run_loss_yesterday": ["python3", "analysis_engine.py", "shop nao hom qua lo"],

        "run_ads_check": ["python3", "ads_report.py", "kiem tra qc"],
    }

    if data in run_map:
        await query.message.reply_text("Dang xu ly, cho mot chut...")
        reply = run_script(run_map[data])
        await send_text_to_message(query.message, reply)
        return

    await query.message.reply_text("Khong co lenh phu hop.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate_interactive_access(update, context):
        return
    register_user_for_broadcast(update)
    raw_text = update.message.text
    text = normalize_text(raw_text)

    # ===== STOCK COMMAND =====
    stock_replies = handle_stock_command(text)
    if stock_replies:
        for msg in stock_replies:
            await update.message.reply_text(msg)
        return

    # ===== BÁN CHẬM =====
    if text == "banchamtatca":
        reply = run_script(["python3", "slow_sales_report.py"])
        await send_text_to_message(update.message, reply)
        return

    if text.startswith("bancham "):
        shop_name = raw_text.strip()[8:].strip()
        reply = run_script(["python3", "slow_sales_by_shop.py", shop_name])
        await send_text_to_message(update.message, reply)
        return

    if text == "banchamtop":
        reply = run_script(["python3", "slow_sales_ranking.py"])
        await send_text_to_message(update.message, reply)
        return

    if text.startswith("xuatsp"):
        args = raw_text.strip()[7:].strip()
        reply = run_script(["python3", "report_sent_items.py", args])
        await send_text_to_message(update.message, reply)
        return

    # ===== LOGIC CŨ =====
    try:
        intent = detect_intent(text)
        cmd = build_command_from_intent(intent, text)
        reply = run_script(cmd)
    except Exception as e:
        reply = f"Loi: {e}"

    await send_text_to_message(update.message, reply)
def main():
    import logging

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.INFO,
    )
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("huongdan", huongdan_command))
    app.add_handler(CommandHandler("help", huongdan_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("shops", shops_command))
    app.add_handler(CommandHandler("sync", sync_command))
    app.add_handler(CommandHandler("addshop", addshop_command))
    app.add_handler(CommandHandler("renameshop", renameshop_command))
    app.add_handler(CommandHandler("kiemtradon", kiemtradon_command))
    app.add_handler(CommandHandler("kiemtraqc", kiemtraqc_command))

    app.add_handler(CallbackQueryHandler(menu_button_callback, pattern="^(menu_|run_|hint_)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Telegram bot dang chay...")
    app.run_polling()


if __name__ == "__main__":
    main()
