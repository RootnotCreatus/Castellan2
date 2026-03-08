import os, sqlite3, time, threading, logging
from dataclasses import dataclass
from math import ceil
from typing import Optional
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import telebot
from telebot import types, apihelper

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def env_str(name:str)->str:
    v=os.getenv(name)
    if v is None or not v.strip(): raise RuntimeError(f"Environment variable {name} is missing")
    return v.strip()

def env_int(name:str)->int:
    try: return int(env_str(name))
    except ValueError as exc: raise RuntimeError(f"Environment variable {name} must be an integer") from exc

BOT_TOKEN=env_str("TG_BOT_TOKEN")
ADMIN_ID=env_int("ADMIN_ID")
GUILD_CHAT_ID=env_int("GUILD_CHAT_ID")
WORKS_THREAD_ID=env_int("WORKS_THREAD_ID")
ABOUT_THREAD_ID=env_int("ABOUT_THREAD_ID")
MASTERS_CHAT_ID=env_int("MASTERS_CHAT_ID")
LEADERS_THREAD_ID=env_int("LEADERS_THREAD_ID")
RESULTS_THREAD_ID=int(os.getenv("RESULTS_THREAD_ID", str(WORKS_THREAD_ID)))
VERIFICATION_THREAD_ID=int(os.getenv("VERIFICATION_THREAD_ID", "1"))
DICE_THREAD_ID=int(os.getenv("DICE_THREAD_ID", "863"))
EVENTS_THREAD_ID=int(os.getenv("EVENTS_THREAD_ID", "989"))
MASTER_IDS=set(int(p.strip()) for p in os.getenv("MASTER_IDS","" ).split(",") if p.strip())

bot=telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
try:
    BOT_USERNAME = bot.get_me().username or ""
except Exception:
    BOT_USERNAME = ""
DB_PATH="guild_bot_stage9.db"
XP_TO_LUX_RATE=10
SHOP_PAGE_SIZE=10
TASK_PAGE_SIZE=10
try:
    MSK=ZoneInfo("Europe/Moscow")
except Exception:
    MSK=timezone(timedelta(hours=3))
    logging.warning("ZoneInfo for Europe/Moscow is unavailable, using fixed UTC+3 fallback")
RARITY_MAP={"common":"⚪️ Обычный","good":"🔵 Добротный","rare":"🟡 Редкий","artful":"🟠 Искусный","relic":"🔴 Реликвия","master":"🟢 Мастерский"}
TITLE_STEPS={0:(0,"без звания"),10:(1000,"Ученик"),20:(2000,"Подмастерье"),30:(4000,"Ремесленник"),40:(8000,"Мастеровой"),50:(16000,"Искусник"),60:(30000,"Старший ремесленник"),70:(60000,"Цеховой"),80:(120000,"Наставник"),90:(250000,"Зодчий"),100:(500000,"Архитектор")}
BOT_STATE_LAST_LEADERS_MESSAGE_ID="last_leaders_message_id"
BOT_STATE_LAST_LEADERS_TEXT="last_leaders_text"
TASK_STATUS_ACTIVE="active"; TASK_STATUS_SUBMITTED="submitted"; TASK_STATUS_APPROVED="approved"; TASK_STATUS_REJECTED="rejected"
DICE_STATUS_OPEN="open"; DICE_STATUS_FINISHED="finished"; DICE_STATUS_EXPIRED="expired"; DICE_STATUS_CANCELLED="cancelled"
DICE_MIN_STAKE=10
DICE_MAX_STAKE=100
DICE_COOLDOWN_SECONDS=180
DICE_WAIT_SECONDS=300
DICE_FEE_PERCENT=4

@dataclass
class LevelState:
    level:int; title:str; current_level_xp_floor:int; next_level_xp_target:int

def now_ts()->int: return int(time.time())
def is_admin_raw(uid:int)->bool: return uid==ADMIN_ID
def get_role_override(uid:int):
    try:
        row=fetchone("SELECT role FROM role_overrides WHERE user_id=?", (uid,))
        return row["role"] if row else None
    except Exception:
        return None

def set_role_override(uid:int, role:Optional[str]):
    if role is None:
        execute("DELETE FROM role_overrides WHERE user_id=?", (uid,))
    else:
        execute("INSERT INTO role_overrides (user_id, role, updated_at) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, updated_at=excluded.updated_at", (uid, role, now_ts()))

def get_effective_role(uid:int)->str:
    override=get_role_override(uid)
    if override in ("newbie","player","master","admin"):
        return override
    if uid==ADMIN_ID:
        return "admin"
    if uid in MASTER_IDS:
        return "master"
    return "player" if get_player(uid) is not None else "newbie"

def is_admin(uid:int)->bool: return get_effective_role(uid)=="admin"
def is_master(uid:int)->bool: return get_effective_role(uid) in ("master","admin")
def is_private_chat(m): return m.chat.type=="private"
def is_works_thread(m): return m.chat.id==GUILD_CHAT_ID and getattr(m,"message_thread_id",None)==WORKS_THREAD_ID
def is_about_thread(m): return m.chat.id==GUILD_CHAT_ID and getattr(m,"message_thread_id",None)==ABOUT_THREAD_ID
def is_verification_thread(m): return m.chat.id==GUILD_CHAT_ID and getattr(m,"message_thread_id",None)==VERIFICATION_THREAD_ID
def has_attachment(m): return any([m.photo,m.document,m.video,m.audio,m.voice])
def get_message_text(m)->str: return (m.text or m.caption or "").strip()
def is_valid_work_message(m)->bool: return has_attachment(m) or ("творение" in get_message_text(m).lower())
def moscow_now()->datetime: return datetime.now(MSK)

def build_level_state(xp:int)->LevelState:
    anchors=sorted(TITLE_STEPS.items(), key=lambda x:x[0])
    if xp<=0: return LevelState(0,TITLE_STEPS[0][1],0,100)
    full=[]
    for i in range(len(anchors)-1):
        sl,(sx,st)=anchors[i]; el,(ex,_)=anchors[i+1]; step=(ex-sx)/(el-sl)
        for lvl in range(sl,el): full.append((lvl,int(round(sx+step*(lvl-sl))),st))
    full.append((100,TITLE_STEPS[100][0],TITLE_STEPS[100][1]))
    cur=full[0]; nxt=(1,100,"без звания")
    for idx,item in enumerate(full):
        if xp>=item[1]: cur=item; nxt=full[idx+1] if idx+1<len(full) else item
        else: break
    return LevelState(cur[0],cur[2],cur[1],nxt[1])

def db_connect():
    conn=sqlite3.connect(DB_PATH)
    conn.row_factory=sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn=db_connect(); cur=conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS players (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, about_text TEXT, xp INTEGER NOT NULL DEFAULT 0, lux INTEGER NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT 'без звания', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, last_work_at INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, source_chat_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL, source_thread_id INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, reviewed_at INTEGER, masters_copy_message_id INTEGER, masters_control_message_id INTEGER, awarded_xp INTEGER, reviewer_id INTEGER, reject_reason TEXT, UNIQUE(source_chat_id, source_message_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS inventory (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, item_id INTEGER, item_name TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 1, acquired_at INTEGER NOT NULL, UNIQUE(user_id, item_id))")
    cur.execute("CREATE TABLE IF NOT EXISTS xp_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, submission_id INTEGER, delta INTEGER NOT NULL, reason TEXT NOT NULL, created_at INTEGER NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS lux_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, delta INTEGER NOT NULL, reason TEXT NOT NULL, created_at INTEGER NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS economy_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, actor_user_id INTEGER, target_user_id INTEGER, resource TEXT NOT NULL, delta INTEGER NOT NULL, reason TEXT NOT NULL, meta TEXT, created_at INTEGER NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS shop_items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, description TEXT NOT NULL, price_lux INTEGER NOT NULL, rarity TEXT NOT NULL, download_url TEXT, is_active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL UNIQUE, description TEXT NOT NULL, reward_lux INTEGER NOT NULL, is_active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS task_claims (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, submitted_at INTEGER, reviewed_at INTEGER, submission_text TEXT, masters_control_message_id INTEGER, reviewer_id INTEGER, reject_reason TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS verifications (user_id INTEGER PRIMARY KEY, welcome_message_id INTEGER, welcome_chat_id INTEGER, welcome_thread_id INTEGER, is_verified INTEGER NOT NULL DEFAULT 0, invited_at INTEGER NOT NULL, verified_at INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS dice_duels (id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER NOT NULL, opponent_id INTEGER, stake_lux INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, accepted_at INTEGER, resolved_at INTEGER, challenge_message_id INTEGER, creator_roll INTEGER, opponent_roll INTEGER, winner_id INTEGER, fee_lux INTEGER NOT NULL DEFAULT 0)")
    cur.execute("CREATE TABLE IF NOT EXISTS role_overrides (user_id INTEGER PRIMARY KEY, role TEXT NOT NULL, updated_at INTEGER NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS master_wallets (user_id INTEGER PRIMARY KEY, seals_tenths INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS master_task_submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, reward_lux INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, reviewed_at INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS master_event_submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, reward_lux INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, reviewed_at INTEGER, event_id INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS master_item_submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL, price_lux INTEGER NOT NULL, rarity TEXT NOT NULL, download_url TEXT, status TEXT NOT NULL, created_at INTEGER NOT NULL, reviewed_at INTEGER, item_id INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS master_events (id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, reward_lux INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, approved_at INTEGER, completed_at INTEGER, event_message_id INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS event_reactions (event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, reacted_at INTEGER NOT NULL, PRIMARY KEY(event_id, user_id))")
    conn.commit(); conn.close()



def get_verification(uid:int):
    return fetchone("SELECT * FROM verifications WHERE user_id=?", (uid,))

def is_verified_user(uid:int)->bool:
    row=get_verification(uid)
    if row is None:
        return True
    return bool(int(row["is_verified"])==1)

def needs_verification(uid:int)->bool:
    row=get_verification(uid)
    return row is not None and int(row["is_verified"])==0

def upsert_verification_invite(uid:int, welcome_message_id:int):
    ts=now_ts()
    execute("INSERT INTO verifications (user_id, welcome_message_id, welcome_chat_id, welcome_thread_id, is_verified, invited_at, verified_at) VALUES (?, ?, ?, ?, 0, ?, NULL) ON CONFLICT(user_id) DO UPDATE SET welcome_message_id=excluded.welcome_message_id, welcome_chat_id=excluded.welcome_chat_id, welcome_thread_id=excluded.welcome_thread_id, invited_at=excluded.invited_at", (uid, welcome_message_id, GUILD_CHAT_ID, VERIFICATION_THREAD_ID, ts))

def mark_user_verified(uid:int):
    ts=now_ts()
    execute(
        """
        INSERT INTO verifications
        (user_id, welcome_message_id, welcome_chat_id, welcome_thread_id, is_verified, invited_at, verified_at)
        VALUES (?, NULL, ?, ?, 1, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            is_verified=1,
            verified_at=excluded.verified_at
        """,
        (uid, GUILD_CHAT_ID, VERIFICATION_THREAD_ID, ts, ts)
    )

def verification_gate_text(user)->str:
    uname='@'+user.username if getattr(user,'username',None) else user.first_name or str(user.id)
    return f"Приветствуем, {uname}! Вы стоите у врат Цитадели Творцов.\n\nЗдесь люди делятся своим творчеством, растут и процветают. Мы ценим чужие таланты.\n\nЧтобы пройти в нашу цитадель — отправь реакцию 💯 на это сообщение."

def send_to_verification_gate(text: str):
    if int(VERIFICATION_THREAD_ID) == 1:
        return bot.send_message(GUILD_CHAT_ID, text)
    return bot.send_message(GUILD_CHAT_ID, text, message_thread_id=VERIFICATION_THREAD_ID)

def send_verification_gate(user):
    old = get_verification(user.id)
    if old and old['welcome_message_id']:
        try:
            bot.delete_message(GUILD_CHAT_ID, int(old['welcome_message_id']))
        except Exception:
            pass
    sent = send_to_verification_gate(verification_gate_text(user))
    upsert_verification_invite(user.id, sent.message_id)
    return sent

def require_verified_message(m, private_only:bool=False)->bool:
    if not needs_verification(m.from_user.id):
        return True
    if private_only and not is_private_chat(m):
        return False
    bot.reply_to(m, "Сначала пройдите верификацию у врат Цитадели: поставьте реакцию 💯 на приветственное сообщение в топике врат.")
    return False

def bot_private_url()->str:
    return f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://t.me"

def about_success_keyboard():
    kb=types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Перейти в бота", url=bot_private_url()))
    return kb

def start_keyboard():
    kb=types.InlineKeyboardMarkup()
    if BOT_USERNAME:
        kb.add(types.InlineKeyboardButton("Команды", url=f"https://t.me/{BOT_USERNAME}?start=commands"))
    return kb

def render_start_text()->str:
    return (
        "<b>Цитадель Творцов</b> — это игра роста внутри гильдии.\n\n"
        "Здесь вы представляете себя, публикуете работы, берёте задания, получаете опыт, превращаете его в люксы, покупаете предметы в лавке и растёте по рангам.\n\n"
        "<b>Правила</b>\n"
        "1. Уважайте чужой труд и чужое время.\n"
        "2. Публикуйте работы по теме и в нужных топиках.\n"
        "3. Критика здесь нужна для роста, а не для унижения.\n"
        "4. Накрутка активности и попытки обмануть систему будут наказываться.\n"
        "5. Главная ценность — не шум, а созидание.\n\n"
        "Список доступных команд — по кнопке ниже или через /command."
    )

# repositories

def fetchone(q, p=()):
    conn=db_connect(); cur=conn.cursor(); cur.execute(q,p); row=cur.fetchone(); conn.close(); return row

def fetchall(q,p=()):
    conn=db_connect(); cur=conn.cursor(); cur.execute(q,p); rows=cur.fetchall(); conn.close(); return rows

def execute(q,p=()):
    conn=db_connect(); cur=conn.cursor(); cur.execute(q,p); conn.commit(); lr=cur.lastrowid; conn.close(); return lr

def get_bot_state(key:str):
    row=fetchone("SELECT value FROM bot_state WHERE key=?", (key,))
    return row["value"] if row else None

def set_bot_state(key:str, value:Optional[str]):
    execute("INSERT INTO bot_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

def delete_bot_state(key:str):
    execute("DELETE FROM bot_state WHERE key=?", (key,))

SEALS_PER_LUX=0.01
MASTER_ITEM_REWARD_TENTHS={"common":20, "good":50, "rare":100, "artful":200, "relic":400, "master":800}

def ensure_master_wallet(uid:int):
    if fetchone("SELECT user_id FROM master_wallets WHERE user_id=?", (uid,)) is None:
        execute("INSERT INTO master_wallets (user_id, seals_tenths, updated_at) VALUES (?, 0, ?)", (uid, now_ts()))

def get_master_wallet(uid:int):
    ensure_master_wallet(uid)
    return fetchone("SELECT * FROM master_wallets WHERE user_id=?", (uid,))

def format_seals_tenths(value:int)->str:
    value=int(value)
    whole=value//10
    frac=value%10
    return f"{whole}.{frac}◈" if frac else f"{whole}◈"

def parse_seals_to_tenths(raw:str)->int:
    raw=raw.replace(",", ".").strip()
    if not raw:
        raise ValueError
    if "." in raw:
        whole, frac = raw.split(".",1)
        if not whole:
            whole='0'
        frac=(frac+'0')[:1]
        return int(whole)*10 + int(frac)
    return int(raw)*10

def update_master_seals(uid:int, delta_tenths:int, reason:str, actor_user_id:Optional[int]=None, meta:Optional[str]=None):
    ensure_master_wallet(uid)
    wallet=get_master_wallet(uid)
    new_value=max(0, int(wallet['seals_tenths'])+int(delta_tenths))
    execute("UPDATE master_wallets SET seals_tenths=?, updated_at=? WHERE user_id=?", (new_value, now_ts(), uid))
    log_economy(actor_user_id, uid, "seal", int(delta_tenths), reason, meta)
    return get_master_wallet(uid)

def user_label_by_id(uid:int)->str:
    p=get_player(uid)
    if p and p['username']:
        return '@'+p['username']
    return f"<code>{uid}</code>"

def announce_master_action(text:str):
    try:
        bot.send_message(GUILD_CHAT_ID, text, message_thread_id=LEADERS_THREAD_ID)
    except Exception:
        logging.exception("[MASTER] failed to announce action")

def create_master_task_submission(creator_id:int, title:str, description:str, reward_lux:int):
    return execute("INSERT INTO master_task_submissions (creator_id, title, description, reward_lux, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)", (creator_id, title, description, reward_lux, now_ts()))

def get_master_task_submission(sub_id:int):
    return fetchone("SELECT * FROM master_task_submissions WHERE id=?", (sub_id,))

def create_master_event_submission(creator_id:int, title:str, description:str, reward_lux:int):
    return execute("INSERT INTO master_event_submissions (creator_id, title, description, reward_lux, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)", (creator_id, title, description, reward_lux, now_ts()))

def get_master_event_submission(sub_id:int):
    return fetchone("SELECT * FROM master_event_submissions WHERE id=?", (sub_id,))

def create_master_item_submission(creator_id:int, name:str, description:str, price_lux:int, rarity:str, download_url:str):
    return execute("INSERT INTO master_item_submissions (creator_id, name, description, price_lux, rarity, download_url, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)", (creator_id, name, description, price_lux, rarity, download_url, now_ts()))

def get_master_item_submission(sub_id:int):
    return fetchone("SELECT * FROM master_item_submissions WHERE id=?", (sub_id,))

def create_master_event(creator_id:int, title:str, description:str, reward_lux:int):
    approved_at=now_ts()
    event_id=execute("INSERT INTO master_events (creator_id, title, description, reward_lux, status, created_at, approved_at) VALUES (?, ?, ?, ?, 'active', ?, ?)", (creator_id, title, description, reward_lux, approved_at, approved_at))
    return get_master_event(event_id)

def get_master_event(event_id:int):
    return fetchone("SELECT * FROM master_events WHERE id=?", (event_id,))

def get_master_event_by_message(message_id:int):
    return fetchone("SELECT * FROM master_events WHERE event_message_id=?", (message_id,))

def set_master_event_message(event_id:int, message_id:int):
    execute("UPDATE master_events SET event_message_id=? WHERE id=?", (message_id, event_id))

def set_event_reaction(event_id:int, uid:int, reacted:bool):
    if reacted:
        execute("INSERT INTO event_reactions (event_id, user_id, reacted_at) VALUES (?, ?, ?) ON CONFLICT(event_id, user_id) DO UPDATE SET reacted_at=excluded.reacted_at", (event_id, uid, now_ts()))
    else:
        execute("DELETE FROM event_reactions WHERE event_id=? AND user_id=?", (event_id, uid))

def has_event_reaction(event_id:int, uid:int)->bool:
    return fetchone("SELECT 1 FROM event_reactions WHERE event_id=? AND user_id=?", (event_id, uid)) is not None

def list_event_reactors(event_id:int):
    return fetchall("SELECT * FROM event_reactions WHERE event_id=? ORDER BY reacted_at ASC", (event_id,))

def render_mastertasks_text(uid:int)->str:
    wallet=get_master_wallet(uid)
    return (
        f"<b>Дела Мастера</b>\n\n"
        f"Ваш баланс: <b>{format_seals_tenths(wallet['seals_tenths'])}</b>\n"
        f"Курс: <b>1◈ = 100❂</b>\n\n"
        f"Заработок:\n"
        f"• одобрение или отказ работы/задачи — 0.1◈\n"
        f"• создание собственной задачи — 10◈\n"
        f"• создание события — 50◈\n"
        f"• добавление предмета — от 2◈ до 80◈ по редкости\n\n"
        f"Команды:\n"
        f"/grant @user xp|lux amount причина\n"
        f"/sealconvert 1.0\n"
        f"/mtask_add\n"
        f"/mevent_add\n"
        f"/mitem_add\n"
        f"/endevent id @user1 @user2 ..."
    )

def send_admin_submission_review(kind:str, sub_id:int, text:str):
    kb=types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f"adminreview:{kind}:approve:{sub_id}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"adminreview:{kind}:reject:{sub_id}")
    )
    bot.send_message(ADMIN_ID, text, reply_markup=kb)

def create_event_post(event_id:int):
    event=get_master_event(event_id)
    text=(
        f"<b>Событие #{event['id']}</b>\n"
        f"{event['title']}\n\n"
        f"{event['description']}\n\n"
        f"Награда: <b>{event['reward_lux']}❂</b>\n"
        f"Чтобы участвовать, отметьтесь реакцией 👍 на это сообщение.\n"
        f"После завершения Мастер укажет тех, кто действительно выполнил событие."
    )
    sent=bot.send_message(GUILD_CHAT_ID, text, message_thread_id=EVENTS_THREAD_ID)
    set_master_event_message(event_id, sent.message_id)
    return sent

def complete_event_and_reward(event_id:int, finisher_id:int, approved_user_ids:list[int]):
    event=get_master_event(event_id)
    if event is None:
        raise RuntimeError("Событие не найдено.")
    if event['status']!='active':
        raise RuntimeError("Событие уже завершено.")
    valid=[]
    for uid in approved_user_ids:
        if has_event_reaction(event_id, uid) and get_player(uid) is not None:
            valid.append(uid)
    for uid in valid:
        update_player_lux(uid, int(event['reward_lux']), 'master_event_reward', finisher_id, f"event_id={event_id}")
    execute("UPDATE master_events SET status='completed', completed_at=? WHERE id=?", (now_ts(), event_id))
    winners=', '.join(user_label_by_id(uid) for uid in valid) if valid else 'никто'
    bot.send_message(GUILD_CHAT_ID, f"<b>Событие #{event_id} завершено.</b>\nНаграждены: {winners}", message_thread_id=EVENTS_THREAD_ID)
    return valid

def grant_from_master_balance(master_id:int, player_id:int, resource:str, amount:int, reason:str):
    if amount<=0:
        raise RuntimeError("Сумма должна быть положительной.")
    cost_tenths=max(1, ceil(amount/10))
    wallet=get_master_wallet(master_id)
    if int(wallet['seals_tenths'])<cost_tenths:
        raise RuntimeError(f"Недостаточно Печатей. Нужно {format_seals_tenths(cost_tenths)}.")
    update_master_seals(master_id, -cost_tenths, f"grant_{resource}", master_id, f"player_id={player_id};reason={reason}")
    if resource=='xp':
        player=update_player_xp(player_id, amount, None, f"master_grant:{reason}", master_id)
    else:
        player=update_player_lux(player_id, amount, f"master_grant:{reason}", master_id)
    announce_master_action(f"<b>Награждение Мастера</b>\nМастер: {user_label_by_id(master_id)}\nИгрок: {player_public_label(player)}\nНаграда: <b>{amount}{'✶' if resource=='xp' else '❂'}</b>\nСписано Печатей: <b>{format_seals_tenths(cost_tenths)}</b>\nПричина: {reason}")
    return player, cost_tenths

def convert_seals_to_lux(uid:int, seals_tenths:int):
    if seals_tenths<=0:
        raise RuntimeError("Нужно указать положительное количество Печатей.")
    wallet=get_master_wallet(uid)
    if int(wallet['seals_tenths'])<seals_tenths:
        raise RuntimeError("Недостаточно Печатей.")
    gross=seals_tenths*10
    fee=max(1, int(round(gross*0.08)))
    net=max(0, gross-fee)
    update_master_seals(uid, -seals_tenths, 'seal_convert', uid, f'gross={gross};fee={fee}')
    player=update_player_lux(uid, net, 'seal_convert', uid, f'fee={fee}')
    return player, gross, fee, net

def assign_master_role(uid:int):
    if uid!=ADMIN_ID:
        set_role_override(uid, 'master')
        ensure_master_wallet(uid)

def master_can_manage_event(uid:int, event)->bool:
    return uid==ADMIN_ID or int(event['creator_id'])==uid

def send_private_if_possible(uid:int, text:str):
    try:
        bot.send_message(uid, text)
    except Exception:
        pass

def get_player(uid:int): return fetchone("SELECT * FROM players WHERE user_id=?", (uid,))
def get_player_by_username(username:str): return fetchone("SELECT * FROM players WHERE lower(username)=?", (username.strip().lstrip("@").lower(),))
def resolve_player_ref(ref:str): return get_player(int(ref)) if ref.strip().lstrip("-").isdigit() else get_player_by_username(ref)

def create_or_update_about(user, about_text:str):
    ts=now_ts(); existing=get_player(user.id)
    if existing is None:
        execute("INSERT INTO players (user_id, username, first_name, about_text, xp, lux, level, title, created_at, updated_at) VALUES (?, ?, ?, ?, 0, 0, 0, 'без звания', ?, ?)", (user.id, user.username or "", user.first_name or "", about_text, ts, ts))
    else:
        execute("UPDATE players SET username=?, first_name=?, about_text=?, updated_at=? WHERE user_id=?", (user.username or "", user.first_name or "", about_text, ts, user.id))
    return get_player(user.id)

def update_player_about_only(uid:int, about_text:str):
    execute("UPDATE players SET about_text=?, updated_at=? WHERE user_id=?", (about_text, now_ts(), uid)); return get_player(uid)

def log_economy(actor,target,res,delta,reason,meta=None): execute("INSERT INTO economy_ledger (actor_user_id,target_user_id,resource,delta,reason,meta,created_at) VALUES (?,?,?,?,?,?,?)", (actor,target,res,delta,reason,meta,now_ts()))

def update_player_xp(uid:int, delta:int, submission_id:Optional[int], reason:str, actor_user_id:Optional[int]=None):
    player=get_player(uid)
    if player is None: raise RuntimeError("Player not found")
    new_xp=max(0,int(player["xp"])+delta); st=build_level_state(new_xp); ts=now_ts()
    execute("UPDATE players SET xp=?, level=?, title=?, updated_at=? WHERE user_id=?", (new_xp, st.level, st.title, ts, uid))
    execute("INSERT INTO xp_log (user_id, submission_id, delta, reason, created_at) VALUES (?, ?, ?, ?, ?)", (uid, submission_id, delta, reason, ts))
    log_economy(actor_user_id, uid, "xp", delta, reason, f"submission_id={submission_id}" if submission_id else None)
    updated=get_player(uid)
    refresh_last_leaders_message_if_exists()
    return updated

def update_player_lux(uid:int, delta:int, reason:str, actor_user_id:Optional[int]=None, meta:Optional[str]=None):
    player=get_player(uid)
    if player is None: raise RuntimeError("Player not found")
    new_lux=max(0,int(player["lux"])+delta); ts=now_ts()
    execute("UPDATE players SET lux=?, updated_at=? WHERE user_id=?", (new_lux, ts, uid))
    execute("INSERT INTO lux_log (user_id, delta, reason, created_at) VALUES (?, ?, ?, ?)", (uid, delta, reason, ts))
    log_economy(actor_user_id, uid, "lux", delta, reason, meta)
    return get_player(uid)

def convert_xp_to_lux(uid:int, xp_amount:int):
    player=get_player(uid)
    if player is None: raise RuntimeError("Player not found")
    if xp_amount<=0 or xp_amount%XP_TO_LUX_RATE!=0: raise RuntimeError(f"Отправьте положительное число, кратное {XP_TO_LUX_RATE}")
    if int(player["xp"])<xp_amount: raise RuntimeError("Недостаточно опыта")
    lux_amount=xp_amount//XP_TO_LUX_RATE
    return update_player_xp(uid,-xp_amount,None,"xp_to_lux",uid), update_player_lux(uid,lux_amount,"xp_to_lux",uid,f"xp_spent={xp_amount}"), lux_amount

def transfer_lux(sender_uid:int, receiver_uid:int, amount:int):
    if sender_uid==receiver_uid: raise RuntimeError("Нельзя переводить себе")
    if amount<=0: raise RuntimeError("Сумма перевода должна быть положительной")
    s=get_player(sender_uid); r=get_player(receiver_uid)
    if s is None or r is None: raise RuntimeError("Игрок не найден")
    if int(s["lux"])<amount: raise RuntimeError("Недостаточно люксов")
    update_player_lux(sender_uid,-amount,"transfer_out",sender_uid,f"to={receiver_uid}")
    update_player_lux(receiver_uid,amount,"transfer_in",sender_uid,f"from={sender_uid}")
    return get_player(sender_uid), get_player(receiver_uid)

def set_last_work_time(uid:int, ts:int): execute("UPDATE players SET last_work_at=?, updated_at=? WHERE user_id=?", (ts, ts, uid))

def create_submission(uid, chat_id, msg_id, thread_id): return execute("INSERT INTO submissions (user_id, source_chat_id, source_message_id, source_thread_id, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)", (uid,chat_id,msg_id,thread_id,now_ts()))
def get_submission(sid:int): return fetchone("SELECT * FROM submissions WHERE id=?", (sid,))
def get_submission_by_control_message(mid:int): return fetchone("SELECT * FROM submissions WHERE masters_control_message_id=?", (mid,))
def link_submission_review_messages(sid:int, copy_mid:int, ctrl_mid:int): execute("UPDATE submissions SET masters_copy_message_id=?, masters_control_message_id=? WHERE id=?", (copy_mid, ctrl_mid, sid))
def set_submission_status(sid:int, status:str, reviewer_id=None, awarded_xp=None, reject_reason=None): execute("UPDATE submissions SET status=?, reviewed_at=?, reviewer_id=?, awarded_xp=?, reject_reason=? WHERE id=?", (status, now_ts(), reviewer_id, awarded_xp, reject_reason, sid))

def rarity_label(code:str)->str: return RARITY_MAP.get(code, code)
def create_shop_item(name,description,price,rarity,download_url=None):
    if rarity not in RARITY_MAP: raise RuntimeError("Unknown rarity")
    ts=now_ts(); item_id=execute("INSERT INTO shop_items (name,description,price_lux,rarity,download_url,is_active,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?)", (name,description,price,rarity,download_url,ts,ts)); return get_shop_item(item_id)
def get_shop_item(item_id:int): return fetchone("SELECT * FROM shop_items WHERE id=?", (item_id,))
def count_active_shop_items()->int: return int(fetchone("SELECT COUNT(*) c FROM shop_items WHERE is_active=1")["c"])
def list_active_shop_items(page:int, page_size:int=SHOP_PAGE_SIZE): return fetchall("SELECT * FROM shop_items WHERE is_active=1 ORDER BY id ASC LIMIT ? OFFSET ?", (page_size,max(0,(page-1)*page_size)))
def update_shop_item_price(item_id:int,new_price:int): execute("UPDATE shop_items SET price_lux=?, updated_at=? WHERE id=?", (new_price,now_ts(),item_id)); return get_shop_item(item_id)
def set_shop_item_active(item_id:int,is_active:bool): execute("UPDATE shop_items SET is_active=?, updated_at=? WHERE id=?", (1 if is_active else 0,now_ts(),item_id)); return get_shop_item(item_id)
def list_inventory(uid:int): return fetchall("SELECT * FROM inventory WHERE user_id=? ORDER BY item_name ASC", (uid,))
def get_inventory_item(uid:int,item_id:int): return fetchone("SELECT * FROM inventory WHERE user_id=? AND item_id=?", (uid,item_id))
def add_inventory_item(uid:int,item_id:int,item_name:str):
    if get_inventory_item(uid,item_id) is None: execute("INSERT INTO inventory (user_id,item_id,item_name,quantity,acquired_at) VALUES (?, ?, ?, 1, ?)", (uid,item_id,item_name,now_ts()))
def buy_shop_item(uid:int,item_id:int):
    player=get_player(uid); item=get_shop_item(item_id)
    if player is None: raise RuntimeError("Игрок не найден")
    if item is None or int(item["is_active"])!=1: raise RuntimeError("Предмет недоступен")
    if get_inventory_item(uid,item_id) is not None: raise RuntimeError("Этот предмет уже есть в вашем инвентаре")
    if int(player["lux"])<int(item["price_lux"]): raise RuntimeError("Недостаточно люксов")
    update_player_lux(uid,-int(item["price_lux"]),"shop_purchase",uid,f"item_id={item_id}")
    add_inventory_item(uid,int(item["id"]),item["name"])
    return get_player(uid), item

def create_task(title, description, reward_lux):
    ts=now_ts(); task_id=execute("INSERT INTO tasks (title,description,reward_lux,is_active,created_at,updated_at) VALUES (?,?,?,1,?,?)", (title,description,reward_lux,ts,ts)); return get_task(task_id)
def get_task(task_id:int): return fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))
def count_active_tasks()->int: return int(fetchone("SELECT COUNT(*) c FROM tasks WHERE is_active=1")["c"])
def list_active_tasks(page:int, page_size:int=TASK_PAGE_SIZE): return fetchall("SELECT * FROM tasks WHERE is_active=1 ORDER BY id ASC LIMIT ? OFFSET ?", (page_size,max(0,(page-1)*page_size)))
def set_task_active(task_id:int,is_active:bool): execute("UPDATE tasks SET is_active=?, updated_at=? WHERE id=?", (1 if is_active else 0, now_ts(), task_id)); return get_task(task_id)
def update_task_reward(task_id:int,reward:int): execute("UPDATE tasks SET reward_lux=?, updated_at=? WHERE id=?", (reward, now_ts(), task_id)); return get_task(task_id)

def get_active_task_claim(uid:int, task_id:int): return fetchone("SELECT * FROM task_claims WHERE user_id=? AND task_id=? AND status IN (?,?) ORDER BY id DESC LIMIT 1", (uid,task_id,TASK_STATUS_ACTIVE,TASK_STATUS_SUBMITTED))
def create_task_claim(task_id:int, uid:int):
    if get_active_task_claim(uid,task_id) is not None: raise RuntimeError("У вас уже есть активное выполнение этого задания")
    cid=execute("INSERT INTO task_claims (task_id,user_id,status,created_at) VALUES (?, ?, ?, ?)", (task_id,uid,TASK_STATUS_ACTIVE,now_ts())); return get_task_claim(cid)
def get_task_claim(cid:int): return fetchone("SELECT * FROM task_claims WHERE id=?", (cid,))
def get_task_claim_by_control_message(mid:int): return fetchone("SELECT * FROM task_claims WHERE masters_control_message_id=?", (mid,))
def get_user_task_history(uid:int): return fetchall("SELECT tc.*, t.title, t.reward_lux FROM task_claims tc JOIN tasks t ON t.id=tc.task_id WHERE tc.user_id=? ORDER BY tc.id DESC LIMIT 20", (uid,))
def submit_task_claim(cid:int, text:str, control_message_id:int): execute("UPDATE task_claims SET status=?, submission_text=?, submitted_at=?, masters_control_message_id=? WHERE id=?", (TASK_STATUS_SUBMITTED,text,now_ts(),control_message_id,cid))
def set_task_claim_status(cid:int,status:str, reviewer_id=None, reject_reason=None): execute("UPDATE task_claims SET status=?, reviewed_at=?, reviewer_id=?, reject_reason=? WHERE id=?", (status,now_ts(),reviewer_id,reject_reason,cid))

def today_msk_bounds_ts():
    now=moscow_now(); start=now.replace(hour=0, minute=0, second=0, microsecond=0); end=start+timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())

def get_task_approved_count_today(uid:int)->int:
    start_ts,end_ts=today_msk_bounds_ts()
    row=fetchone("SELECT COUNT(*) c FROM task_claims WHERE user_id=? AND status=? AND reviewed_at>=? AND reviewed_at<?", (uid,TASK_STATUS_APPROVED,start_ts,end_ts))
    return int(row["c"])

def get_leaders(limit:int=20):
    start_ts,end_ts=today_msk_bounds_ts()
    return fetchall("SELECT p.user_id,p.username,p.title,p.xp,p.level, COALESCE(SUM(CASE WHEN tc.status=? AND tc.reviewed_at>=? AND tc.reviewed_at<? THEN 1 ELSE 0 END),0) tasks_today FROM players p LEFT JOIN task_claims tc ON tc.user_id=p.user_id GROUP BY p.user_id,p.username,p.title,p.xp,p.level ORDER BY p.xp DESC, p.level DESC LIMIT ?", (TASK_STATUS_APPROVED,start_ts,end_ts,limit))

def get_open_dice_duel_by_creator(uid:int):
    return fetchone("SELECT * FROM dice_duels WHERE creator_id=? AND status=? ORDER BY id DESC LIMIT 1", (uid, DICE_STATUS_OPEN))

def get_open_dice_duel_for_user(uid:int):
    return fetchone("SELECT * FROM dice_duels WHERE status=? AND (creator_id=? OR opponent_id=?) ORDER BY id DESC LIMIT 1", (DICE_STATUS_OPEN, uid, uid))

def get_last_dice_created_at(uid:int)->Optional[int]:
    row=fetchone("SELECT created_at FROM dice_duels WHERE creator_id=? ORDER BY id DESC LIMIT 1", (uid,))
    return int(row["created_at"]) if row else None

def create_dice_duel(creator_id:int, stake_lux:int, challenge_message_id:int)->int:
    return execute("INSERT INTO dice_duels (creator_id, stake_lux, status, created_at, challenge_message_id) VALUES (?, ?, ?, ?, ?)", (creator_id, stake_lux, DICE_STATUS_OPEN, now_ts(), challenge_message_id))

def get_dice_duel(duel_id:int):
    return fetchone("SELECT * FROM dice_duels WHERE id=?", (duel_id,))

def set_dice_duel_message_id(duel_id:int, message_id:int):
    execute("UPDATE dice_duels SET challenge_message_id=? WHERE id=?", (message_id, duel_id))

def accept_dice_duel(duel_id:int, opponent_id:int):
    execute("UPDATE dice_duels SET opponent_id=?, accepted_at=? WHERE id=?", (opponent_id, now_ts(), duel_id))

def finish_dice_duel(duel_id:int, creator_roll:int, opponent_roll:int, winner_id:Optional[int], fee_lux:int):
    execute("UPDATE dice_duels SET status=?, resolved_at=?, creator_roll=?, opponent_roll=?, winner_id=?, fee_lux=? WHERE id=?", (DICE_STATUS_FINISHED, now_ts(), creator_roll, opponent_roll, winner_id, fee_lux, duel_id))

def set_dice_duel_status(duel_id:int, status:str):
    execute("UPDATE dice_duels SET status=?, resolved_at=? WHERE id=?", (status, now_ts(), duel_id))

def list_expired_open_dice_duels():
    threshold=now_ts()-DICE_WAIT_SECONDS
    return fetchall("SELECT * FROM dice_duels WHERE status=? AND created_at<?", (DICE_STATUS_OPEN, threshold))

def dice_pot_and_fee(stake_lux:int):
    pot=stake_lux*2
    fee=max(1, int(round(pot*DICE_FEE_PERCENT/100)))
    if fee>=pot:
        fee=pot-1
    return pot, fee, pot-fee

def user_label_by_id(uid:int)->str:
    p=get_player(uid)
    if p and p["username"]:
        return "@" + p["username"]
    if p:
        return f"<code>{p['user_id']}</code>"
    return f"<code>{uid}</code>"

# views

def render_profile_text(player):
    st=build_level_state(int(player["xp"])); about=player["about_text"] or "—"
    return f"<b>{player['title']}, {player['level']} уровень</b> (<code>{player['xp']}/{st.next_level_xp_target}✶</code>)\n\nБаланс люксов: <b>{player['lux']}❂</b>\n\nО вас: {about}"

def profile_keyboard():
    kb=types.InlineKeyboardMarkup(); kb.add(types.InlineKeyboardButton("Изменить информацию о себе", callback_data="profile:edit_about")); kb.add(types.InlineKeyboardButton("Инвентарь", callback_data="profile:inventory")); return kb

def render_inventory_text(uid:int):
    items=list_inventory(uid)
    if not items: return "Инвентарь пуст."
    return "\n".join(["<b>Ваш инвентарь</b>"]+[f"• {i['item_name']}" for i in items])

def inventory_keyboard(uid:int):
    items=list_inventory(uid)
    if not items: return None
    kb=types.InlineKeyboardMarkup()
    for i in items: kb.add(types.InlineKeyboardButton(i["item_name"], callback_data=f"inventory:view:{i['item_id']}"))
    return kb

def render_inventory_item(uid:int,item_id:int):
    inv=get_inventory_item(uid,item_id)
    if inv is None: raise RuntimeError("Item not found in inventory")
    item=get_shop_item(item_id)
    if item is None: raise RuntimeError("Shop item not found")
    text=f"<b>{item['name']}</b>\n{rarity_label(item['rarity'])}\n\n{item['description']}"
    if item['download_url']: text += f"\n\nСсылка на скачивание: {item['download_url']}"
    kb=types.InlineKeyboardMarkup(); kb.add(types.InlineKeyboardButton("Назад в инвентарь", callback_data="inventory:back")); return text,kb

def render_shop_page(page:int):
    total=count_active_shop_items(); total_pages=max(1,ceil(total/SHOP_PAGE_SIZE)); page=max(1,min(page,total_pages)); items=list_active_shop_items(page)
    kb=types.InlineKeyboardMarkup(); lines=[f"<b>Лавка Мастеров</b>\nСтраница {page}/{total_pages}"]
    for item in items:
        lines.append(f"\n<b>{item['id']}. {item['name']}</b>\n{rarity_label(item['rarity'])} • {item['price_lux']}❂")
        kb.add(types.InlineKeyboardButton(item['name'], callback_data=f"shop:view:{item['id']}:{page}"))
    nav=[]
    if page>1: nav.append(types.InlineKeyboardButton("←", callback_data=f"shop:page:{page-1}"))
    if page<total_pages: nav.append(types.InlineKeyboardButton("→", callback_data=f"shop:page:{page+1}"))
    if nav: kb.row(*nav)
    return "\n".join(lines),kb

def render_shop_item(item,page:int):
    text=f"<b>{item['name']}</b>\n{rarity_label(item['rarity'])}\nЦена: <b>{item['price_lux']}❂</b>\n\n{item['description']}"
    kb=types.InlineKeyboardMarkup(); kb.add(types.InlineKeyboardButton("Купить", callback_data=f"shop:buy:{item['id']}:{page}")); kb.add(types.InlineKeyboardButton("Назад", callback_data=f"shop:page:{page}")); return text,kb

def tasks_intro_text():
    return ("<b>Задания Гильдии</b>\n\n"
            "Здесь вы можете брать задания, отправлять их на проверку Мастерам и получать награды в люксах.\n"
            "История показывает ваши последние попытки и результаты.")

def tasks_intro_keyboard():
    kb=types.InlineKeyboardMarkup(); kb.add(types.InlineKeyboardButton("Список задач", callback_data="tasksmenu:list:1")); kb.add(types.InlineKeyboardButton("История задач", callback_data="tasksmenu:history:1")); return kb

def render_tasks_page(page:int):
    total=count_active_tasks(); total_pages=max(1,ceil(total/TASK_PAGE_SIZE)); page=max(1,min(page,total_pages)); tasks=list_active_tasks(page)
    kb=types.InlineKeyboardMarkup(); lines=[f"<b>Список задач</b>\nСтраница {page}/{total_pages}"]
    for task in tasks:
        lines.append(f"\n<b>{task['id']}. {task['title']}</b>\nНаграда: {task['reward_lux']}❂")
        kb.add(types.InlineKeyboardButton(task['title'], callback_data=f"tasks:view:{task['id']}:{page}"))
    nav=[]
    if page>1: nav.append(types.InlineKeyboardButton("←", callback_data=f"tasks:page:{page-1}"))
    if page<total_pages: nav.append(types.InlineKeyboardButton("→", callback_data=f"tasks:page:{page+1}"))
    if nav: kb.row(*nav)
    kb.add(types.InlineKeyboardButton("↩︎ В меню заданий", callback_data="tasksmenu:home:1"))
    return "\n".join(lines),kb

def render_task(task,page:int,uid:int):
    claim=get_active_task_claim(uid,int(task['id']))
    status_line=f"\n\nВаш статус: <b>{claim['status']}</b>" if claim is not None else ""
    text=f"<b>{task['title']}</b>\nНаграда: <b>{task['reward_lux']}❂</b>\n\n{task['description']}{status_line}"
    kb=types.InlineKeyboardMarkup()
    if claim is None: kb.add(types.InlineKeyboardButton("Взять задание", callback_data=f"tasks:claim:{task['id']}:{page}"))
    elif claim['status']==TASK_STATUS_ACTIVE: kb.add(types.InlineKeyboardButton("Отправить выполнение", callback_data=f"tasks:submit:{claim['id']}:{page}"))
    kb.add(types.InlineKeyboardButton("Назад", callback_data=f"tasks:page:{page}"))
    return text,kb

def render_task_history(uid:int):
    history=get_user_task_history(uid)
    if not history: return "<b>История задач</b>\n\nПока пусто.", tasks_intro_keyboard()
    lines=["<b>История задач</b>"]
    for row in history[:20]: lines.append(f"• {row['title']} — <b>{row['status']}</b>")
    kb=types.InlineKeyboardMarkup(); kb.add(types.InlineKeyboardButton("↩︎ В меню заданий", callback_data="tasksmenu:home:1"))
    return "\n".join(lines), kb

def task_review_keyboard(claim_id:int):
    kb=types.InlineKeyboardMarkup(); kb.add(types.InlineKeyboardButton("Подтвердить задание", callback_data=f"taskreview:approve:{claim_id}")); kb.add(types.InlineKeyboardButton("Отклонить с причиной", callback_data=f"taskreview:reject:{claim_id}")); return kb

def review_keyboard(submission_id:int):
    kb=types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Оценить", callback_data=f"review:score:{submission_id}"))
    kb.add(types.InlineKeyboardButton("Отклонить с причиной", callback_data=f"review:reject:{submission_id}"))
    return kb

def render_leaders():
    raw_rows = get_leaders(50)
    rows = [row for row in raw_rows if not is_master(int(row["user_id"]))][:10]
    lines = ["<b>Доска почётных творцов:</b>", ""]
    if not rows:
        lines.append("Пока нет данных.")
        return "\n".join(lines)

    for idx, row in enumerate(rows, start=1):
        name = '@' + row['username'] if row['username'] else str(row['user_id'])
        st = build_level_state(int(row['xp']))
        progress = f"{row['xp']}/{st.next_level_xp_target}✶"
        lines.append(f"{idx}. {name}, {row['title']}, Уровень ({progress})")
        lines.append(f"Выполнено заданий: {row['tasks_today']}")
        if idx != len(rows):
            lines.append("")

    return "\n".join(lines)

def dice_challenge_text(duel):
    creator=user_label_by_id(int(duel["creator_id"]))
    pot, fee, payout = dice_pot_and_fee(int(duel["stake_lux"]))
    return (
        f"<b>Стол костей #{duel['id']}</b>\n"
        f"Создатель: {creator}\n"
        f"Ставка: <b>{duel['stake_lux']}❂</b>\n"
        f"Банк: <b>{pot}❂</b> • Победитель получит <b>{payout}❂</b>\n"
        f"Пошлина лавки: <b>{fee}❂</b>\n"
        f"Ожидание соперника: <b>5 минут</b>"
    )

def dice_keyboard(duel_id:int, creator_id:int):
    kb=types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Принять вызов", callback_data=f"dice:accept:{duel_id}"))
    kb.add(types.InlineKeyboardButton("Отменить", callback_data=f"dice:cancel:{duel_id}:{creator_id}"))
    return kb

# state
pending_score_input={}
pending_about_edit=set(); pending_convert_xp=set()
pending_admin_shop_create={}; pending_admin_task_create={}; pending_task_submit={}
pending_master_task_create={}; pending_master_event_create={}; pending_master_item_create={}
pending_art_reject_reason={}; pending_task_reject_reason={}
prompt_messages={}  # (kind,user_id)->message_id
last_leaders_sent_date=None

# helpers

def safe_send(chat_id:int, text:str, **kwargs):
    return bot.send_message(chat_id, text, **kwargs)

def safe_delete(chat_id:int, message_id:Optional[int]):
    if not message_id: return
    try: bot.delete_message(chat_id, message_id)
    except Exception: pass

def send_prompt(kind:str, user_id:int, text:str):
    old=prompt_messages.get((kind,user_id));
    if old: safe_delete(user_id, old)
    m=bot.send_message(user_id, text)
    prompt_messages[(kind,user_id)] = m.message_id
    return m

def clear_prompt(kind:str, user_id:int):
    old=prompt_messages.pop((kind,user_id), None)
    safe_delete(user_id, old)

def delete_later(chat_id:int, message_id:int, delay:float=6.0):
    def _job():
        try:
            time.sleep(delay)
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    threading.Thread(target=_job, daemon=True).start()

def temp_reply(m, text:str, delay:float=6.0):
    try:
        sent = bot.reply_to(m, text)
        delete_later(sent.chat.id, sent.message_id, delay)
        return sent
    except Exception:
        return None

def notify_results(text:str):
    try:
        bot.send_message(GUILD_CHAT_ID, text, message_thread_id=RESULTS_THREAD_ID)
    except Exception:
        logging.exception("[RESULTS] failed to send results message")

def player_public_label(player)->str:
    if player is None:
        return "неизвестный игрок"
    if player['username']:
        return '@' + player['username']
    return f"<code>{player['user_id']}</code>"

def actor_public_label(user)->str:
    if user is None:
        return "Мастер"
    if getattr(user, 'username', None):
        return '@' + user.username
    return f"<code>{user.id}</code>"

def ensure_dice_access(m)->bool:
    if not require_verified_message(m):
        return False
    p=get_player(m.from_user.id)
    if p is None:
        temp_reply(m, "Профиль не найден. Сначала расскажите о себе в теме знакомства.")
        return False
    return True

def cooldown_left_text(seconds:int)->str:
    mins=seconds//60
    secs=seconds%60
    return f"{mins} мин. {secs} сек." if mins else f"{secs} сек."

def create_dice_challenge_and_lock(creator_id:int, stake:int):
    update_player_lux(creator_id, -stake, "dice_reserve", creator_id, f"stake={stake}")
    duel_id=create_dice_duel(creator_id, stake, 0)
    duel=get_dice_duel(duel_id)
    msg=bot.send_message(GUILD_CHAT_ID, dice_challenge_text(duel), message_thread_id=DICE_THREAD_ID, reply_markup=dice_keyboard(duel_id, creator_id))
    set_dice_duel_message_id(duel_id, msg.message_id)
    return get_dice_duel(duel_id), msg

def cancel_dice_duel_and_refund(duel_id:int, status:str, reason_text:str):
    duel=get_dice_duel(duel_id)
    if duel is None or duel["status"]!=DICE_STATUS_OPEN:
        return False
    update_player_lux(int(duel["creator_id"]), int(duel["stake_lux"]), "dice_refund", None, f"duel_id={duel_id}")
    set_dice_duel_status(duel_id, status)
    try:
        bot.edit_message_text(reason_text, GUILD_CHAT_ID, int(duel["challenge_message_id"]))
    except Exception:
        try:
            bot.send_message(GUILD_CHAT_ID, reason_text, message_thread_id=DICE_THREAD_ID)
        except Exception:
            pass
    return True

def resolve_dice_duel(duel_id:int, opponent_id:int):
    duel=get_dice_duel(duel_id)
    if duel is None or duel["status"]!=DICE_STATUS_OPEN:
        raise RuntimeError("Дуэль уже недоступна")
    creator_id=int(duel["creator_id"])
    stake=int(duel["stake_lux"])
    creator=get_player(creator_id)
    opponent=get_player(opponent_id)
    if opponent is None or creator is None:
        raise RuntimeError("Игрок не найден")
    if int(opponent["lux"])<stake:
        raise RuntimeError("У вас недостаточно люксов для принятия ставки")
    update_player_lux(opponent_id, -stake, "dice_reserve", opponent_id, f"duel_id={duel_id}")
    accept_dice_duel(duel_id, opponent_id)
    try:
        bot.edit_message_text(
            f"<b>Стол костей #{duel_id}</b>\n{user_label_by_id(creator_id)} против {user_label_by_id(opponent_id)}\nСтавка: <b>{stake}❂</b>\n\nБросаем кости...",
            GUILD_CHAT_ID,
            int(duel["challenge_message_id"])
        )
    except Exception:
        pass

    first=bot.send_dice(GUILD_CHAT_ID, emoji="🎲", message_thread_id=DICE_THREAD_ID)
    second=bot.send_dice(GUILD_CHAT_ID, emoji="🎲", message_thread_id=DICE_THREAD_ID)
    creator_roll=int(first.dice.value)
    opponent_roll=int(second.dice.value)

    # Даём Telegram проиграть анимацию кубиков, а затем публикуем итог отдельным сообщением.
    time.sleep(4.2)

    pot, fee, payout=dice_pot_and_fee(stake)
    winner_id=None
    result_lines=[
        f"<b>Стол костей #{duel_id}</b>",
        f"{user_label_by_id(creator_id)}: 🎲 <b>{creator_roll}</b>",
        f"{user_label_by_id(opponent_id)}: 🎲 <b>{opponent_roll}</b>",
        ""
    ]
    if creator_roll>opponent_roll:
        winner_id=creator_id
        update_player_lux(creator_id, payout, "dice_win", opponent_id, f"duel_id={duel_id};pot={pot};fee={fee}")
        result_lines.append(f"Победитель: {user_label_by_id(creator_id)}")
        result_lines.append(f"Награда: <b>{payout}❂</b>")
        result_lines.append(f"Пошлина лавки: <b>{fee}❂</b>")
    elif opponent_roll>creator_roll:
        winner_id=opponent_id
        update_player_lux(opponent_id, payout, "dice_win", creator_id, f"duel_id={duel_id};pot={pot};fee={fee}")
        result_lines.append(f"Победитель: {user_label_by_id(opponent_id)}")
        result_lines.append(f"Награда: <b>{payout}❂</b>")
        result_lines.append(f"Пошлина лавки: <b>{fee}❂</b>")
    else:
        fee=0
        update_player_lux(creator_id, stake, "dice_refund_draw", None, f"duel_id={duel_id}")
        update_player_lux(opponent_id, stake, "dice_refund_draw", None, f"duel_id={duel_id}")
        result_lines.append("Ничья. Ставки возвращены обоим игрокам.")
    finish_dice_duel(duel_id, creator_roll, opponent_roll, winner_id, fee)
    result_text="\n".join(result_lines)
    bot.send_message(GUILD_CHAT_ID, result_text, message_thread_id=DICE_THREAD_ID)
    try:
        if duel["challenge_message_id"]:
            bot.delete_message(GUILD_CHAT_ID, int(duel["challenge_message_id"]))
    except Exception:
        pass
    return True

def expire_dice_duels_loop():
    while True:
        try:
            for duel in list_expired_open_dice_duels():
                cancel_dice_duel_and_refund(
                    int(duel["id"]),
                    DICE_STATUS_EXPIRED,
                    f"<b>Стол костей #{duel['id']}</b>\nВызов истёк. {user_label_by_id(int(duel['creator_id']))} получает обратно <b>{duel['stake_lux']}❂</b>."
                )
        except Exception:
            logging.exception("[DICE] expire loop failed")
        time.sleep(15)

COMMANDS_REGISTRY = [
    {"name": "/start", "desc": "описание игры", "roles": ("newbie", "player", "master", "admin")},
    {"name": "/command", "desc": "список доступных команд", "roles": ("newbie", "player", "master", "admin")},
    {"name": "/profile", "desc": "профиль", "roles": ("player", "master", "admin")},
    {"name": "/tasks", "desc": "раздел задач", "roles": ("player", "master", "admin")},
    {"name": "/convert", "desc": "конвертация XP в люксы", "roles": ("player", "master", "admin")},
    {"name": "/transfer", "desc": "перевод люксов", "roles": ("player", "master", "admin")},
    {"name": "/taberna", "desc": "лавка", "roles": ("player", "master", "admin")},
    {"name": "/dice", "desc": "дуэль в кости в топике игр", "roles": ("player", "master", "admin")},
    {"name": "/leaders", "desc": "таблица лидеров", "roles": ("master", "admin")},
    {"name": "/mastertasks", "desc": "список мастерских дел", "roles": ("master", "admin")},
    {"name": "/grant", "desc": "наградить игрока из Печатей", "roles": ("master", "admin")},
    {"name": "/sealconvert", "desc": "конвертировать Печати в люксы", "roles": ("master", "admin")},
    {"name": "/mtask_add", "desc": "предложить задание", "roles": ("master", "admin")},
    {"name": "/mevent_add", "desc": "предложить событие", "roles": ("master", "admin")},
    {"name": "/mitem_add", "desc": "предложить предмет", "roles": ("master", "admin")},
    {"name": "/endevent", "desc": "завершить событие", "roles": ("master", "admin")},
    {"name": "/penaltyxp", "desc": "штраф по XP", "roles": ("master", "admin")},
    {"name": "/penaltylux", "desc": "штраф по люксам", "roles": ("master", "admin")},
    {"name": "/grantxp", "desc": "админское начисление XP", "roles": ("admin",)},
    {"name": "/grantlux", "desc": "админское начисление люксов", "roles": ("admin",)},
    {"name": "/shop_add", "desc": "добавить предмет", "roles": ("admin",)},
    {"name": "/shop_price", "desc": "изменить цену предмета", "roles": ("admin",)},
    {"name": "/shop_hide", "desc": "скрыть предмет", "roles": ("admin",)},
    {"name": "/shop_show", "desc": "показать предмет", "roles": ("admin",)},
    {"name": "/shop_link", "desc": "задать ссылку предмета", "roles": ("admin",)},
    {"name": "/task_add", "desc": "добавить задание", "roles": ("admin",)},
    {"name": "/task_hide", "desc": "скрыть задание", "roles": ("admin",)},
    {"name": "/task_show", "desc": "показать задание", "roles": ("admin",)},
    {"name": "/task_reward", "desc": "изменить награду задания", "roles": ("admin",)},
    {"name": "/setrole", "desc": "назначить себе роль", "roles": ("admin",)},
]

ROLE_TITLES = {
    "newbie": "Команды новичка",
    "player": "Команды игрока",
    "master": "Команды Мастера",
    "admin": "Команды админа",
}


def get_user_command_roles(uid:int):
    eff=get_effective_role(uid)
    if eff=='newbie':
        return ['newbie']
    roles=['player']
    if eff in ('master','admin'):
        roles.append('master')
    if eff=='admin':
        roles.append('admin')
    return roles


def render_commands_for_user(uid:int)->str:
    user_roles = set(get_user_command_roles(uid))
    sections = []
    for role in ('newbie', 'player', 'master', 'admin'):
        if role not in user_roles:
            continue
        lines = [f"{item['name']} — {item['desc']}" for item in COMMANDS_REGISTRY if role in item['roles']]
        if lines:
            sections.append(f"<b>{ROLE_TITLES[role]}</b>\n" + "\n".join(lines))
    return "\n\n".join(sections)

def announce_penalty(text:str):
    try:
        bot.send_message(GUILD_CHAT_ID, text, message_thread_id=LEADERS_THREAD_ID)
    except Exception:
        logging.exception("[PENALTY] failed to announce penalty")

def get_last_leaders_message_id()->Optional[int]:
    raw=get_bot_state(BOT_STATE_LAST_LEADERS_MESSAGE_ID)
    if raw is None or str(raw).strip()=="":
        return None
    try:
        return int(raw)
    except Exception:
        return None

def set_last_leaders_message_id(message_id:Optional[int]):
    if message_id is None:
        delete_bot_state(BOT_STATE_LAST_LEADERS_MESSAGE_ID)
    else:
        set_bot_state(BOT_STATE_LAST_LEADERS_MESSAGE_ID, str(int(message_id)))




def get_last_leaders_text()->Optional[str]:
    return get_bot_state(BOT_STATE_LAST_LEADERS_TEXT)

def set_last_leaders_text(text:Optional[str]):
    if text is None:
        delete_bot_state(BOT_STATE_LAST_LEADERS_TEXT)
    else:
        set_bot_state(BOT_STATE_LAST_LEADERS_TEXT, text)

def create_leaders_snapshot_message():
    text = render_leaders()
    msg=bot.send_message(GUILD_CHAT_ID, text, message_thread_id=LEADERS_THREAD_ID)
    set_last_leaders_message_id(msg.message_id)
    set_last_leaders_text(text)
    return msg


def refresh_last_leaders_message():
    message_id=get_last_leaders_message_id()
    if not message_id:
        return False
    new_text = render_leaders()
    old_text = get_last_leaders_text()
    if old_text == new_text:
        logging.info("[LEADERS] skip refresh: text is not changed")
        return False
    try:
        bot.edit_message_text(
            chat_id=GUILD_CHAT_ID,
            message_id=message_id,
            text=new_text
        )
        set_last_leaders_text(new_text)
        return True
    except Exception as e:
        if "message is not modified" in str(e).lower():
            set_last_leaders_text(new_text)
            logging.info("[LEADERS] skip refresh: Telegram reports message is not modified")
            return False
        logging.exception("[LEADERS] failed to refresh last leaders message")
        return False


def refresh_last_leaders_message_if_exists():
    if get_last_leaders_message_id() is None:
        return
    refresh_last_leaders_message()

def forward_submission_to_masters(m, submission_id:int):
    logging.info(f"[REVIEW] start submission_id={submission_id}, from_chat_id={m.chat.id}, message_id={m.message_id}")
    copied = bot.copy_message(chat_id=MASTERS_CHAT_ID, from_chat_id=m.chat.id, message_id=m.message_id)
    logging.info(f"[REVIEW] copy_message ok, copied_message_id={getattr(copied, 'message_id', None)}")
    kb = review_keyboard(submission_id)
    logging.info(f"[REVIEW] keyboard created for submission_id={submission_id}")
    control = bot.send_message(
        MASTERS_CHAT_ID,
        f"<b>Заявка #{submission_id}</b>\nИгрок: @{m.from_user.username or m.from_user.id}\nСтатус: <b>pending</b>",
        reply_markup=kb
    )
    logging.info(f"[REVIEW] control message sent, control_message_id={control.message_id}")
    link_submission_review_messages(submission_id, getattr(copied, 'message_id', None), control.message_id)
    logging.info(f"[REVIEW] linked review messages for submission_id={submission_id}")
    return copied, control

def announce_leaders(force=False):
    global last_leaders_sent_date
    now=moscow_now(); today=now.date().isoformat()
    if not force:
        if now.hour!=21:
            return
        if last_leaders_sent_date==today:
            return
    try:
        create_leaders_snapshot_message()
        last_leaders_sent_date=today
    except Exception:
        logging.exception("[LEADERS] failed to announce leaders")

def leaders_scheduler_loop():
    while True:
        try:
            refresh_last_leaders_message_if_exists()
            announce_leaders(False)
        except Exception:
            logging.exception("[LEADERS] scheduler loop failed")
        time.sleep(30)



@bot.message_handler(content_types=["new_chat_members"])
def handle_new_chat_members(m):
    if m.chat.id!=GUILD_CHAT_ID:
        return
    for user in (m.new_chat_members or []):
        if getattr(user, "is_bot", False):
            continue
        try:
            send_verification_gate(user)
        except Exception:
            logging.exception(f"[VERIFY] failed to send gate message for user_id={user.id}")


@bot.message_reaction_handler(func=lambda r: True)
def handle_message_reaction(r):
    try:
        if getattr(r.chat, 'id', None) != GUILD_CHAT_ID:
            return
        uid = getattr(getattr(r, 'user', None), 'id', None)
        if not uid:
            return

        message_id = int(getattr(r, 'message_id', 0) or 0)

        def has_emoji(reactions, emoji: str) -> bool:
            for reaction in (reactions or []):
                if getattr(reaction, 'type', None) == 'emoji' and getattr(reaction, 'emoji', None) == emoji:
                    return True
            return False

        # 1) Участие в событиях: фиксируем реакцию 👍 на сообщении события
        event = get_master_event_by_message(message_id) if message_id else None
        if event is not None:
            reacted_now = has_emoji(getattr(r, 'new_reaction', None), '👍')
            set_event_reaction(int(event['id']), int(uid), reacted_now)
            logging.info(f"[EVENT] reaction sync event_id={event['id']} user_id={uid} reacted={reacted_now}")
            return

        # 2) Верификация у врат: реакция 💯
        row = get_verification(uid)
        if row is None or int(row['is_verified']) == 1:
            return
        if int(row['welcome_message_id'] or 0) != message_id:
            return
        if not has_emoji(getattr(r, 'new_reaction', None), '💯'):
            return

        mark_user_verified(uid)
        try:
            bot.delete_message(GUILD_CHAT_ID, int(row['welcome_message_id']))
        except Exception:
            pass
        logging.info(f"[VERIFY] user verified via reaction user_id={uid}")
    except Exception:
        logging.exception("[REACTION] message reaction handler failed")
# commands
@bot.message_handler(func=lambda m: m.chat.id==GUILD_CHAT_ID and getattr(m, "message_thread_id", None)==DICE_THREAD_ID and bool(get_message_text(m).strip().startswith("/")) and not get_message_text(m).strip().startswith("/dice"), content_types=["text"])
def delete_non_dice_commands_in_dice_thread(m):
    try:
        bot.delete_message(m.chat.id, m.message_id)
    except Exception:
        pass

@bot.message_handler(commands=["start"])
def cmd_start(m):
    if needs_verification(m.from_user.id):
        bot.reply_to(m, "Сначала пройдите верификацию у врат Цитадели: поставьте реакцию 💯 на приветственное сообщение в топике врат.")
        return
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)>1 and parts[1].strip()=="commands":
        bot.reply_to(m, render_commands_for_user(m.from_user.id))
        return
    bot.reply_to(m, render_start_text(), reply_markup=start_keyboard())

@bot.message_handler(commands=["command"])
def cmd_command(m):
    if not require_verified_message(m): return
    bot.reply_to(m, render_commands_for_user(m.from_user.id))

@bot.message_handler(commands=["profile"])
def cmd_profile(m):
    if not is_private_chat(m):
        bot.reply_to(m, "Команда /profile доступна только в боте, в личных сообщениях.")
        return
    if not require_verified_message(m): return
    p=get_player(m.from_user.id)
    if p is None:
        bot.reply_to(m, "Профиль не найден. Сначала расскажите о себе в теме знакомства.")
        return
    bot.reply_to(m, render_profile_text(p), reply_markup=profile_keyboard())

@bot.message_handler(commands=["taberna"])
def cmd_taberna(m):
    if not require_verified_message(m): return
    p=get_player(m.from_user.id)
    if p is None: bot.reply_to(m, "Профиль не найден. Сначала расскажите о себе в теме знакомства."); return
    text,kb=render_shop_page(1); bot.reply_to(m, text, reply_markup=kb)

@bot.message_handler(commands=["tasks"])
def cmd_tasks(m):
    if not require_verified_message(m): return
    p=get_player(m.from_user.id)
    if p is None: bot.reply_to(m, "Профиль не найден. Сначала расскажите о себе в теме знакомства."); return
    bot.reply_to(m, tasks_intro_text(), reply_markup=tasks_intro_keyboard())

@bot.message_handler(commands=["dice"])
def cmd_dice(m):
    if not ensure_dice_access(m): return
    if m.chat.id!=GUILD_CHAT_ID or getattr(m,'message_thread_id',None)!=DICE_THREAD_ID:
        temp_reply(m, f"Эту команду используйте в игровом топике {DICE_THREAD_ID}.")
        return
    parts=get_message_text(m).split()
    if len(parts)!=2:
        temp_reply(m, f"Формат: /dice сумма\nСтавка от {DICE_MIN_STAKE} до {DICE_MAX_STAKE} люксов.")
        return
    try:
        stake=int(parts[1])
    except ValueError:
        temp_reply(m, "Ставка должна быть целым числом.")
        return
    if stake < DICE_MIN_STAKE or stake > DICE_MAX_STAKE:
        temp_reply(m, f"Ставка должна быть от {DICE_MIN_STAKE} до {DICE_MAX_STAKE} люксов.")
        return
    player=get_player(m.from_user.id)
    if int(player['lux']) < stake:
        temp_reply(m, "Недостаточно люксов для такой ставки.")
        return
    open_duel=get_open_dice_duel_for_user(m.from_user.id)
    if open_duel is not None:
        temp_reply(m, "У вас уже есть активная дуэль. Сначала дождитесь её завершения или отмените её.")
        return
    last_created=get_last_dice_created_at(m.from_user.id)
    if last_created is not None:
        left=DICE_COOLDOWN_SECONDS-(now_ts()-last_created)
        if left>0:
            bot.reply_to(m, f"Перед новым вызовом нужно подождать {cooldown_left_text(left)}.")
            return
    try:
        duel,_=create_dice_challenge_and_lock(m.from_user.id, stake)
    except Exception:
        logging.exception("[DICE] failed to create challenge")
        bot.reply_to(m, "Не удалось открыть стол костей.")
        return
    bot.reply_to(m, f"Вызов создан. {stake}❂ зарезервированы на 5 минут. Стол #{duel['id']}.")

@bot.message_handler(commands=["leaders"])
def cmd_leaders(m):
    if not require_verified_message(m): return
    if not is_master(m.from_user.id):
        bot.reply_to(m, "Команда доступна только Мастерам."); return
    if m.chat.id!=GUILD_CHAT_ID or getattr(m,'message_thread_id',None)!=LEADERS_THREAD_ID:
        bot.reply_to(m, "Эту команду используйте в топике лидеров."); return
    try:
        create_leaders_snapshot_message()
    except Exception:
        logging.exception("[LEADERS] failed to create leaders snapshot by command")
        bot.reply_to(m, "Не удалось обновить таблицу лидеров.")

@bot.message_handler(commands=["convert"])
def cmd_convert(m):
    if not require_verified_message(m): return
    p=get_player(m.from_user.id)
    if p is None: bot.reply_to(m, "Профиль не найден. Сначала расскажите о себе в теме знакомства."); return
    pending_convert_xp.add(m.from_user.id); send_prompt("convert", m.from_user.id, f"Введите количеством XP, которое хотите конвертировать в люксы. Курс: {XP_TO_LUX_RATE}✶ = 1❂")

@bot.message_handler(commands=["transfer"])
def cmd_transfer(m):
    if not require_verified_message(m): return
    sender=get_player(m.from_user.id)
    if sender is None: bot.reply_to(m, "Профиль не найден. Сначала расскажите о себе в теме знакомства."); return
    parts=get_message_text(m).split()
    if len(parts)!=3: bot.reply_to(m, "Формат: /transfer @username количество"); return
    receiver=resolve_player_ref(parts[1])
    if receiver is None: bot.reply_to(m, "Получатель не найден. Проверьте @username."); return
    try: amount=int(parts[2])
    except ValueError: bot.reply_to(m, "Количество люксов должно быть целым числом."); return
    try: s,r=transfer_lux(int(sender['user_id']), int(receiver['user_id']), amount)
    except RuntimeError as exc: bot.reply_to(m, str(exc)); return
    s_label='@'+s['username'] if s['username'] else f"<code>{s['user_id']}</code>"; r_label='@'+r['username'] if r['username'] else f"<code>{r['user_id']}</code>"
    bot.reply_to(m, f"Перевод выполнен: {amount}❂ → {r_label}.\nВаш новый баланс: {s['lux']}❂")
    try: bot.send_message(int(r['user_id']), f"{s_label} перевёл вам {amount}❂.\nВаш новый баланс: {r['lux']}❂")
    except Exception: pass

@bot.message_handler(commands=["debug"])
def cmd_debug(m):
    if not is_admin(m.from_user.id): return
    bot.reply_to(m, f"chat_id=<code>{m.chat.id}</code>\nthread_id=<code>{getattr(m,'message_thread_id',None)}</code>\nuser_id=<code>{m.from_user.id}</code>")

@bot.message_handler(commands=["setrole"])
def cmd_setrole(m):
    if not is_admin_raw(m.from_user.id):
        return
    parts=get_message_text(m).split()
    if len(parts)!=2 or parts[1] not in ('newbie','player','master','admin','clear'):
        bot.reply_to(m, 'Формат: /setrole newbie|player|master|admin|clear')
        return
    role=None if parts[1]=='clear' else parts[1]
    set_role_override(m.from_user.id, role)
    if role=='master':
        ensure_master_wallet(m.from_user.id)
    elif role=='newbie':
        try:
            gate = send_verification_gate(m.from_user)
            bot.reply_to(m, f'Роль переключена: {role or "по умолчанию"}. Сообщение у врат отправлено (id {gate.message_id}).')
            return
        except Exception as e:
            logging.exception(f"[VERIFY] failed to send gate message after /setrole newbie for user_id={m.from_user.id}")
            bot.reply_to(
                m,
                f'Роль переключена: newbie. Ошибка отправки у врат: {e}\nchat_id={GUILD_CHAT_ID}, verification_thread_id={VERIFICATION_THREAD_ID}'
            )
            return
    bot.reply_to(m, f'Роль переключена: {role or "по умолчанию"}.')

@bot.message_handler(commands=["mastertasks"])
def cmd_mastertasks(m):
    if not is_master(m.from_user.id):
        return
    bot.reply_to(m, render_mastertasks_text(m.from_user.id))

@bot.message_handler(commands=["grant"])
def cmd_grant(m):
    if not is_master(m.from_user.id):
        return
    parts=get_message_text(m).split(maxsplit=4)
    if len(parts)!=5:
        bot.reply_to(m, 'Формат: /grant @username xp|lux amount причина')
        return
    p=resolve_player_ref(parts[1])
    if p is None:
        bot.reply_to(m, 'Игрок не найден.')
        return
    resource=parts[2].lower()
    if resource not in ('xp','lux'):
        bot.reply_to(m, 'Нужно указать xp или lux.')
        return
    try:
        amount=int(parts[3])
    except ValueError:
        bot.reply_to(m, 'amount должен быть целым числом.')
        return
    reason=parts[4].strip()
    try:
        player, cost=grant_from_master_balance(m.from_user.id, int(p['user_id']), resource, amount, reason)
    except RuntimeError as exc:
        bot.reply_to(m, str(exc))
        return
    bot.reply_to(m, f'Награда выдана. Списано Печатей: {format_seals_tenths(cost)}.')

@bot.message_handler(commands=["sealconvert"])
def cmd_sealconvert(m):
    if not is_master(m.from_user.id):
        return
    parts=get_message_text(m).split()
    if len(parts)!=2:
        bot.reply_to(m, 'Формат: /sealconvert 1.0')
        return
    try:
        seals_tenths=parse_seals_to_tenths(parts[1])
        player, gross, fee, net = convert_seals_to_lux(m.from_user.id, seals_tenths)
    except Exception as exc:
        bot.reply_to(m, str(exc))
        return
    bot.reply_to(m, f'Конвертация завершена. Списано {format_seals_tenths(seals_tenths)} → начислено {net}❂ (комиссия {fee}❂). Баланс: {player["lux"]}❂')

@bot.message_handler(commands=["mtask_add"])
def cmd_mtask_add(m):
    if not is_master(m.from_user.id):
        return
    pending_master_task_create[m.from_user.id]={'step':'title','data':{}}
    send_prompt('mtask_add', m.from_user.id, 'Создание мастерского задания. Отправьте название.')

@bot.message_handler(commands=["mevent_add"])
def cmd_mevent_add(m):
    if not is_master(m.from_user.id):
        return
    pending_master_event_create[m.from_user.id]={'step':'title','data':{}}
    send_prompt('mevent_add', m.from_user.id, 'Создание события. Отправьте название.')

@bot.message_handler(commands=["mitem_add"])
def cmd_mitem_add(m):
    if not is_master(m.from_user.id):
        return
    pending_master_item_create[m.from_user.id]={'step':'name','data':{}}
    send_prompt('mitem_add', m.from_user.id, 'Создание предмета. Отправьте название.')

@bot.message_handler(commands=["endevent"])
def cmd_endevent(m):
    if not is_master(m.from_user.id):
        return
    parts=get_message_text(m).split()
    if len(parts)<3:
        bot.reply_to(m, 'Формат: /endevent id @user1 @user2 ...')
        return
    try:
        event_id=int(parts[1])
    except ValueError:
        bot.reply_to(m, 'id события должен быть числом.')
        return
    event=get_master_event(event_id)
    if event is None:
        bot.reply_to(m, 'Событие не найдено.')
        return
    if not master_can_manage_event(m.from_user.id, event):
        bot.reply_to(m, 'Завершить событие может только его автор или админ.')
        return
    approved=[]
    for ref in parts[2:]:
        p=resolve_player_ref(ref)
        if p is not None:
            approved.append(int(p['user_id']))
    if not approved:
        bot.reply_to(m, 'Не удалось распознать ни одного игрока для награждения.')
        return
    try:
        valid=complete_event_and_reward(event_id, m.from_user.id, approved)
    except RuntimeError as exc:
        bot.reply_to(m, str(exc))
        return
    bot.reply_to(m, f'Событие завершено. Награждены: {", ".join(user_label_by_id(uid) for uid in valid) if valid else "никто"}.')

@bot.message_handler(commands=["start"])
def cmd_start(m):
    if is_private_chat(m):
        if needs_verification(m.from_user.id):
            bot.reply_to(m, 'Сначала пройдите верификацию у врат Цитадели: поставьте реакцию 💯 на приветственное сообщение в топике врат.')
            return
        bot.send_message(m.chat.id, render_start_text(), reply_markup=start_keyboard())
        return

@bot.message_handler(commands=["grantxp"])
def cmd_grantxp(m):
    if not is_admin(m.from_user.id): return
    parts=get_message_text(m).split()
    if len(parts)!=3: bot.reply_to(m, "Формат: /grantxp @username число"); return
    try: delta=int(parts[2])
    except ValueError: bot.reply_to(m, "Число XP должно быть целым."); return
    p=resolve_player_ref(parts[1])
    if p is None: bot.reply_to(m, "Игрок не найден."); return
    u=update_player_xp(int(p['user_id']), delta, None, "admin_adjustment", m.from_user.id); label='@'+u['username'] if u['username'] else f"<code>{u['user_id']}</code>"
    bot.reply_to(m, f"XP обновлены. Игрок {label}: {u['xp']}✶, уровень {u['level']}, звание {u['title']}")

@bot.message_handler(commands=["grantlux"])
def cmd_grantlux(m):
    if not is_admin(m.from_user.id): return
    parts=get_message_text(m).split()
    if len(parts)!=3: bot.reply_to(m, "Формат: /grantlux @username число"); return
    try: delta=int(parts[2])
    except ValueError: bot.reply_to(m, "Число люксов должно быть целым."); return
    p=resolve_player_ref(parts[1])
    if p is None: bot.reply_to(m, "Игрок не найден."); return
    u=update_player_lux(int(p['user_id']), delta, "admin_adjustment", m.from_user.id); label='@'+u['username'] if u['username'] else f"<code>{u['user_id']}</code>"
    bot.reply_to(m, f"Люксы обновлены. Игрок {label}: {u['lux']}❂")


@bot.message_handler(commands=["penaltyxp"])
def cmd_penaltyxp(m):
    if not is_master(m.from_user.id): return
    parts=get_message_text(m).split(maxsplit=3)
    if len(parts)!=4:
        bot.reply_to(m, "Формат: /penaltyxp @username число причина")
        return
    p=resolve_player_ref(parts[1])
    if p is None:
        bot.reply_to(m, "Игрок не найден.")
        return
    try:
        amount=int(parts[2])
    except ValueError:
        bot.reply_to(m, "Число XP должно быть целым.")
        return
    if amount<=0:
        bot.reply_to(m, "Размер штрафа должен быть положительным.")
        return
    reason=parts[3].strip()
    if not reason:
        bot.reply_to(m, "Нужно указать причину штрафа.")
        return
    before=int(p['xp'])
    u=update_player_xp(int(p['user_id']), -amount, None, f"penalty_xp:{reason}", m.from_user.id)
    actual_penalty=max(0, before-int(u['xp']))
    label=player_public_label(u)
    bot.reply_to(m, f"XP оштрафованы. Игрок {label}: {u['xp']}✶, уровень {u['level']}, звание {u['title']}")
    announce_penalty(
        f"⚠️ {label} получил штраф <b>{actual_penalty}✶</b>.\n"
        f"Причина: {reason}\n"
        f"Мастер: {actor_public_label(m.from_user)}"
    )


@bot.message_handler(commands=["penaltylux"])
def cmd_penaltylux(m):
    if not is_master(m.from_user.id): return
    parts=get_message_text(m).split(maxsplit=3)
    if len(parts)!=4:
        bot.reply_to(m, "Формат: /penaltylux @username число причина")
        return
    p=resolve_player_ref(parts[1])
    if p is None:
        bot.reply_to(m, "Игрок не найден.")
        return
    try:
        amount=int(parts[2])
    except ValueError:
        bot.reply_to(m, "Число люксов должно быть целым.")
        return
    if amount<=0:
        bot.reply_to(m, "Размер штрафа должен быть положительным.")
        return
    reason=parts[3].strip()
    if not reason:
        bot.reply_to(m, "Нужно указать причину штрафа.")
        return
    before=int(p['lux'])
    u=update_player_lux(int(p['user_id']), -amount, f"penalty_lux:{reason}", m.from_user.id)
    actual_penalty=max(0, before-int(u['lux']))
    label=player_public_label(u)
    bot.reply_to(m, f"Люксы оштрафованы. Игрок {label}: {u['lux']}❂")
    announce_penalty(
        f"⚠️ {label} получил штраф <b>{actual_penalty}❂</b>.\n"
        f"Причина: {reason}\n"
        f"Мастер: {actor_public_label(m.from_user)}"
    )


@bot.message_handler(commands=["shop_add"])
def cmd_shop_add(m):
    if not is_admin(m.from_user.id): return
    pending_admin_shop_create[m.from_user.id]={'step':'name','data':{}}; send_prompt("shop_add", m.from_user.id, "Создание предмета. Отправьте название.")

@bot.message_handler(commands=["shop_price"])
def cmd_shop_price(m):
    if not is_admin(m.from_user.id): return
    parts=get_message_text(m).split(maxsplit=2)
    if len(parts)!=3: bot.reply_to(m, "Формат: /shop_price item_id новая_цена"); return
    try: item_id=int(parts[1]); price=int(parts[2])
    except ValueError: bot.reply_to(m, "item_id и цена должны быть числами."); return
    item=update_shop_item_price(item_id,price)
    if item is None: bot.reply_to(m, "Предмет не найден."); return
    bot.reply_to(m, f"Цена обновлена: {item['name']} = {item['price_lux']}❂")

@bot.message_handler(commands=["shop_hide"])
def cmd_shop_hide(m):
    if not is_admin(m.from_user.id): return
    parts=get_message_text(m).split()
    if len(parts)!=2: bot.reply_to(m, "Формат: /shop_hide item_id"); return
    try: item_id=int(parts[1])
    except ValueError: bot.reply_to(m, "item_id должен быть числом."); return
    item=set_shop_item_active(item_id,False)
    if item is None: bot.reply_to(m, "Предмет не найден."); return
    bot.reply_to(m, f"Снято с продажи: {item['name']}")

@bot.message_handler(commands=["shop_show"])
def cmd_shop_show(m):
    if not is_admin(m.from_user.id): return
    parts=get_message_text(m).split()
    if len(parts)!=2: bot.reply_to(m, "Формат: /shop_show item_id"); return
    try: item_id=int(parts[1])
    except ValueError: bot.reply_to(m, "item_id должен быть числом."); return
    item=set_shop_item_active(item_id,True)
    if item is None: bot.reply_to(m, "Предмет не найден."); return
    bot.reply_to(m, f"Возвращено в лавку: {item['name']}")

@bot.message_handler(commands=["shop_link"])
def cmd_shop_link(m):
    if not is_admin(m.from_user.id): return
    parts=get_message_text(m).split(maxsplit=2)
    if len(parts)!=3: bot.reply_to(m, "Формат: /shop_link item_id ссылка\nЧтобы убрать ссылку: /shop_link item_id -"); return
    try: item_id=int(parts[1])
    except ValueError: bot.reply_to(m, "item_id должен быть числом."); return
    download_url=None if parts[2].strip()=="-" else parts[2].strip(); execute("UPDATE shop_items SET download_url=?, updated_at=? WHERE id=?", (download_url, now_ts(), item_id)); item=get_shop_item(item_id)
    if item is None: bot.reply_to(m, "Предмет не найден."); return
    bot.reply_to(m, f"Ссылка {'обновлена' if download_url else 'удалена'} для предмета: {item['name']}")

@bot.message_handler(commands=["task_add"])
def cmd_task_add(m):
    if not is_admin(m.from_user.id): return
    pending_admin_task_create[m.from_user.id]={'step':'title','data':{}}; send_prompt("task_add", m.from_user.id, "Создание задания. Отправьте название.")

@bot.message_handler(commands=["task_hide"])
def cmd_task_hide(m):
    if not is_admin(m.from_user.id): return
    parts=get_message_text(m).split()
    if len(parts)!=2: bot.reply_to(m, "Формат: /task_hide task_id"); return
    try: task_id=int(parts[1])
    except ValueError: bot.reply_to(m, "task_id должен быть числом."); return
    task=set_task_active(task_id,False)
    if task is None: bot.reply_to(m, "Задание не найдено."); return
    bot.reply_to(m, f"Скрыто задание: {task['title']}")

@bot.message_handler(commands=["task_show"])
def cmd_task_show(m):
    if not is_admin(m.from_user.id): return
    parts=get_message_text(m).split()
    if len(parts)!=2: bot.reply_to(m, "Формат: /task_show task_id"); return
    try: task_id=int(parts[1])
    except ValueError: bot.reply_to(m, "task_id должен быть числом."); return
    task=set_task_active(task_id,True)
    if task is None: bot.reply_to(m, "Задание не найдено."); return
    bot.reply_to(m, f"Возвращено задание: {task['title']}")

@bot.message_handler(commands=["task_reward"])
def cmd_task_reward(m):
    if not is_admin(m.from_user.id): return
    parts=get_message_text(m).split()
    if len(parts)!=3: bot.reply_to(m, "Формат: /task_reward task_id новая_награда"); return
    try: task_id=int(parts[1]); reward=int(parts[2])
    except ValueError: bot.reply_to(m, "task_id и награда должны быть числами."); return
    task=update_task_reward(task_id,reward)
    if task is None: bot.reply_to(m, "Задание не найдено."); return
    bot.reply_to(m, f"Награда обновлена: {task['title']} = {task['reward_lux']}❂")

# main handler
@bot.message_handler(content_types=["text","photo","document","video","audio","voice","sticker","animation"])
def handle_messages(m):
    if m.from_user is None: return
    if is_private_chat(m):
        if not is_verified_user(m.from_user.id):
            bot.reply_to(m, "Бот станет доступен после верификации у врат Цитадели. Поставьте реакцию 💯 на приветственное сообщение в топике врат.")
            return
        if m.from_user.id in pending_about_edit:
            txt=get_message_text(m)
            if not txt: bot.reply_to(m, "Нужно отправить текстом новое описание о себе."); return
            p=get_player(m.from_user.id)
            if p is None: pending_about_edit.discard(m.from_user.id); clear_prompt("about",m.from_user.id); bot.reply_to(m, "Профиль не найден. Сначала расскажите о себе в теме знакомства."); return
            up=update_player_about_only(m.from_user.id,txt); pending_about_edit.discard(m.from_user.id); clear_prompt("about",m.from_user.id); safe_delete(m.chat.id,m.message_id); bot.send_message(m.chat.id, "Описание обновлено."); bot.send_message(m.chat.id, render_profile_text(up), reply_markup=profile_keyboard()); return
        if m.from_user.id in pending_convert_xp:
            raw=get_message_text(m)
            if not raw.isdigit(): bot.reply_to(m, f"Нужно отправить положительное число, кратное {XP_TO_LUX_RATE}."); return
            try: xp_p,lux_p,lux_amt=convert_xp_to_lux(m.from_user.id,int(raw))
            except RuntimeError as exc: bot.reply_to(m, str(exc)); return
            pending_convert_xp.discard(m.from_user.id); clear_prompt("convert",m.from_user.id); safe_delete(m.chat.id,m.message_id)
            bot.send_message(m.chat.id, f"Конвертация выполнена: {raw}✶ → {lux_amt}❂\nНовый баланс: {lux_p['lux']}❂\nОставшийся опыт: {xp_p['xp']}✶"); return
        if m.from_user.id in pending_task_submit:
            claim_id=pending_task_submit[m.from_user.id]; claim=get_task_claim(claim_id)
            if claim is None or claim['status']!=TASK_STATUS_ACTIVE: pending_task_submit.pop(m.from_user.id,None); clear_prompt("task_submit",m.from_user.id); bot.reply_to(m, "Эта заявка на задание уже недоступна."); return
            txt=get_message_text(m)
            if not txt: bot.reply_to(m, "Нужно отправить текстом описание выполнения задания."); return
            task=get_task(int(claim['task_id']))
            ctrl=bot.send_message(MASTERS_CHAT_ID, f"<b>Выполнение задания #{claim['id']}</b>\nИгрок: <code>{claim['user_id']}</code>\nЗадание: <b>{task['title'] if task else claim['task_id']}</b>\n\n{txt}", reply_markup=task_review_keyboard(int(claim['id'])))
            submit_task_claim(claim_id, txt, ctrl.message_id); pending_task_submit.pop(m.from_user.id,None); clear_prompt("task_submit",m.from_user.id); safe_delete(m.chat.id,m.message_id)
            bot.send_message(m.chat.id, "Выполнение задания отправлено Мастерам на рассмотрение.")
            return

        if m.from_user.id in pending_master_task_create and is_master(m.from_user.id):
            state=pending_master_task_create[m.from_user.id]; txt=get_message_text(m)
            if not txt:
                bot.reply_to(m, "Нужно отправить текстом ответ на текущий шаг.")
                return
            if state['step']=='title':
                state['data']['title']=txt
                state['step']='description'
                send_prompt("mtask_add", m.from_user.id, "Теперь отправьте описание задания.")
                return
            if state['step']=='description':
                state['data']['description']=txt
                state['step']='reward'
                send_prompt("mtask_add", m.from_user.id, "Теперь отправьте награду в люксах (целое число).")
                return
            if state['step']=='reward':
                if not txt.isdigit():
                    bot.reply_to(m, "Награда должна быть целым положительным числом.")
                    return
                sub_id=create_master_task_submission(m.from_user.id, state['data']['title'], state['data']['description'], int(txt))
                pending_master_task_create.pop(m.from_user.id, None)
                clear_prompt("mtask_add", m.from_user.id)
                send_admin_submission_review(
                    'mtask',
                    sub_id,
                    f"<b>Заявка мастера на задание #{sub_id}</b>\n"
                    f"Мастер: <code>{m.from_user.id}</code>\n"
                    f"Название: <b>{state['data']['title']}</b>\n"
                    f"Награда: <b>{int(txt)}❂</b>\n\n"
                    f"{state['data']['description']}"
                )
                bot.send_message(m.chat.id, f"Заявка на задание отправлена админу на проверку. Номер: #{sub_id}")
                return

        if m.from_user.id in pending_master_event_create and is_master(m.from_user.id):
            state=pending_master_event_create[m.from_user.id]; txt=get_message_text(m)
            if not txt:
                bot.reply_to(m, "Нужно отправить текстом ответ на текущий шаг.")
                return
            if state['step']=='title':
                state['data']['title']=txt
                state['step']='description'
                send_prompt("mevent_add", m.from_user.id, "Теперь отправьте описание события.")
                return
            if state['step']=='description':
                state['data']['description']=txt
                state['step']='reward'
                send_prompt("mevent_add", m.from_user.id, "Теперь отправьте награду в люксах (целое число).")
                return
            if state['step']=='reward':
                if not txt.isdigit():
                    bot.reply_to(m, "Награда должна быть целым положительным числом.")
                    return
                sub_id=create_master_event_submission(m.from_user.id, state['data']['title'], state['data']['description'], int(txt))
                pending_master_event_create.pop(m.from_user.id, None)
                clear_prompt("mevent_add", m.from_user.id)
                send_admin_submission_review(
                    'mevent',
                    sub_id,
                    f"<b>Заявка мастера на событие #{sub_id}</b>\n"
                    f"Мастер: <code>{m.from_user.id}</code>\n"
                    f"Название: <b>{state['data']['title']}</b>\n"
                    f"Награда: <b>{int(txt)}❂</b>\n\n"
                    f"{state['data']['description']}"
                )
                bot.send_message(m.chat.id, f"Заявка на событие отправлена админу на проверку. Номер: #{sub_id}")
                return

        if m.from_user.id in pending_master_item_create and is_master(m.from_user.id):
            state=pending_master_item_create[m.from_user.id]; txt=get_message_text(m)
            if not txt:
                bot.reply_to(m, "Нужно отправить текстом ответ на текущий шаг.")
                return
            if state['step']=='name':
                state['data']['name']=txt
                state['step']='description'
                send_prompt("mitem_add", m.from_user.id, "Теперь отправьте описание предмета.")
                return
            if state['step']=='description':
                state['data']['description']=txt
                state['step']='price'
                send_prompt("mitem_add", m.from_user.id, "Теперь отправьте цену в люксах (целое число).")
                return
            if state['step']=='price':
                if not txt.isdigit():
                    bot.reply_to(m, "Цена должна быть целым положительным числом.")
                    return
                state['data']['price_lux']=int(txt)
                state['step']='rarity'
                send_prompt("mitem_add", m.from_user.id, "Теперь отправьте редкость: common / good / rare / artful / relic / master")
                return
            if state['step']=='rarity':
                rarity=txt.strip().lower()
                if rarity not in RARITY_MAP:
                    bot.reply_to(m, "Неизвестная редкость. Используйте: common / good / rare / artful / relic / master")
                    return
                state['data']['rarity']=rarity
                state['step']='download_url'
                send_prompt("mitem_add", m.from_user.id, "Теперь отправьте ссылку на скачивание. Если ссылки нет, отправьте -")
                return
            if state['step']=='download_url':
                url="" if txt.strip()=="-" else txt.strip()
                sub_id=create_master_item_submission(
                    m.from_user.id,
                    state['data']['name'],
                    state['data']['description'],
                    int(state['data']['price_lux']),
                    state['data']['rarity'],
                    url
                )
                pending_master_item_create.pop(m.from_user.id, None)
                clear_prompt("mitem_add", m.from_user.id)
                send_admin_submission_review(
                    'mitem',
                    sub_id,
                    f"<b>Заявка мастера на предмет #{sub_id}</b>\n"
                    f"Мастер: <code>{m.from_user.id}</code>\n"
                    f"Название: <b>{state['data']['name']}</b>\n"
                    f"Цена: <b>{int(state['data']['price_lux'])}❂</b>\n"
                    f"Редкость: <b>{rarity_label(state['data']['rarity'])}</b>\n"
                    f"Ссылка: {url or '-'}\n\n"
                    f"{state['data']['description']}"
                )
                bot.send_message(m.chat.id, f"Заявка на предмет отправлена админу на проверку. Номер: #{sub_id}")
                return

        if m.from_user.id in pending_admin_shop_create and is_admin(m.from_user.id):
            state=pending_admin_shop_create[m.from_user.id]; txt=get_message_text(m)
            if not txt: bot.reply_to(m, "Нужно отправить текстом ответ на текущий шаг."); return
            if state['step']=='name': state['data']['name']=txt; state['step']='description'; send_prompt("shop_add",m.from_user.id,"Теперь отправьте описание предмета."); return
            if state['step']=='description': state['data']['description']=txt; state['step']='price'; send_prompt("shop_add",m.from_user.id,"Теперь отправьте цену в люксах (целое число)."); return
            if state['step']=='price':
                if not txt.isdigit(): bot.reply_to(m, "Цена должна быть целым положительным числом."); return
                state['data']['price_lux']=int(txt); state['step']='rarity'; send_prompt("shop_add",m.from_user.id,"Теперь отправьте редкость: common / good / rare / artful / relic / master"); return
            if state['step']=='rarity':
                rarity=txt.strip().lower()
                if rarity not in RARITY_MAP: bot.reply_to(m, "Неизвестная редкость. Используйте: common / good / rare / artful / relic / master"); return
                state['data']['rarity']=rarity; state['step']='download_url'; send_prompt("shop_add",m.from_user.id,"Теперь отправьте ссылку на скачивание. Если ссылки нет, отправьте -"); return
            if state['step']=='download_url':
                url=None if txt.strip()=="-" else txt.strip(); item=create_shop_item(state['data']['name'],state['data']['description'],state['data']['price_lux'],state['data']['rarity'],url)
                pending_admin_shop_create.pop(m.from_user.id,None); clear_prompt("shop_add",m.from_user.id); bot.send_message(m.chat.id, f"Предмет создан: #{item['id']} {item['name']} • {rarity_label(item['rarity'])} • {item['price_lux']}❂"); return
        if m.from_user.id in pending_admin_task_create and is_admin(m.from_user.id):
            state=pending_admin_task_create[m.from_user.id]; txt=get_message_text(m)
            if not txt: bot.reply_to(m, "Нужно отправить текстом ответ на текущий шаг."); return
            if state['step']=='title': state['data']['title']=txt; state['step']='description'; send_prompt("task_add",m.from_user.id,"Теперь отправьте описание задания."); return
            if state['step']=='description': state['data']['description']=txt; state['step']='reward'; send_prompt("task_add",m.from_user.id,"Теперь отправьте награду в люксах (целое число)."); return
            if state['step']=='reward':
                if not txt.isdigit(): bot.reply_to(m, "Награда должна быть целым положительным числом."); return
                task=create_task(state['data']['title'],state['data']['description'],int(txt)); pending_admin_task_create.pop(m.from_user.id,None); clear_prompt("task_add",m.from_user.id); bot.send_message(m.chat.id, f"Задание создано: #{task['id']} {task['title']} • {task['reward_lux']}❂"); return
        return
    if m.chat.id==MASTERS_CHAT_ID and not is_master(m.from_user.id):
        assign_master_role(m.from_user.id)
    if m.chat.id==GUILD_CHAT_ID and getattr(m, 'message_thread_id', None)==DICE_THREAD_ID:
        txt=get_message_text(m).strip()
        if txt.startswith('/dice'):
            return
        try:
            bot.delete_message(m.chat.id, m.message_id)
        except Exception:
            pass
        return
    if is_about_thread(m):
        if not is_verified_user(m.from_user.id):
            try: bot.delete_message(m.chat.id, m.message_id)
            except Exception: pass
            send_to_verification_gate(f"@{m.from_user.username or m.from_user.id}, сначала подтвердите вход реакцией 💯 у врат Цитадели.")
            return
        txt=get_message_text(m)
        if not txt: bot.reply_to(m, "Нужно кратко рассказать о себе текстом."); return
        create_or_update_about(m.from_user, txt); bot.reply_to(m, "Добро пожаловать! Теперь вы можете ознакомиться с правилами нашей игры в нашем боте.", reply_markup=about_success_keyboard()); return
    if is_works_thread(m):
        if not is_verified_user(m.from_user.id):
            bot.reply_to(m, "Сначала подтвердите вход реакцией 💯 у врат Цитадели, затем представьтесь в теме знакомств."); return
        p=get_player(m.from_user.id)
        if p is None: bot.reply_to(m, "Сначала расскажите о себе в теме знакомства."); return
        if not is_valid_work_message(m):
            try: bot.delete_message(m.chat.id,m.message_id)
            except Exception: pass
            return
        ts=now_ts(); last=p['last_work_at']
        if last is not None and ts-int(last)<60: bot.reply_to(m, "Можно отправлять только одно творение в минуту."); return
        set_last_work_time(m.from_user.id, ts); sid=create_submission(m.from_user.id,m.chat.id,m.message_id,m.message_thread_id)
        bot.reply_to(m, f"✨ Спасибо, что творите, @{m.from_user.username or m.from_user.id}. Вашу работу скоро оценят наши Мастера.")
        copy=bot.copy_message(chat_id=MASTERS_CHAT_ID, from_chat_id=m.chat.id, message_id=m.message_id)
        ctrl=bot.send_message(MASTERS_CHAT_ID, f"<b>Заявка #{sid}</b>\nИгрок: @{m.from_user.username or m.from_user.id}\nСтатус: <b>pending</b>", reply_markup=review_keyboard(sid))
        link_submission_review_messages(sid, copy.message_id, ctrl.message_id); return
    if m.chat.id==MASTERS_CHAT_ID and is_master(m.from_user.id):
        if m.reply_to_message is None: return
        # work reject reason
        if m.from_user.id in pending_art_reject_reason:
            sid=pending_art_reject_reason[m.from_user.id]; sub=get_submission(sid)
            if sub is None or sub['status']!='pending': pending_art_reject_reason.pop(m.from_user.id,None); bot.reply_to(m, "Эта работа уже обработана."); return
            reason=get_message_text(m).strip()
            if not reason: bot.reply_to(m, "Нужно написать причину отказа текстом."); return
            player=get_player(int(sub['user_id'])); title=player['title'] if player else 'без звания'; username=player['username'] if player and player['username'] else str(sub['user_id'])
            set_submission_status(sid, 'rejected', reviewer_id=m.from_user.id, reject_reason=reason); update_master_seals(m.from_user.id, 1, 'review_reject_work', m.from_user.id, f'submission_id={sid}'); pending_art_reject_reason.pop(m.from_user.id,None)
            text=f"{title} @{username}, Мастера отклонили вашу работу. Причина: {reason}"
            try: bot.send_message(int(sub['user_id']), text)
            except Exception: pass
            notify_results(text)
            try: bot.edit_message_text(chat_id=MASTERS_CHAT_ID, message_id=sub['masters_control_message_id'], text=f"<b>Заявка #{sid}</b>\nИгрок: <code>{sub['user_id']}</code>\nСтатус: <b>rejected</b>\nПричина: {reason}\nМастер: <code>{m.from_user.id}</code>")
            except Exception: pass
            return
        # task reject reason
        if m.from_user.id in pending_task_reject_reason:
            cid=pending_task_reject_reason[m.from_user.id]; claim=get_task_claim(cid)
            if claim is None or claim['status']!=TASK_STATUS_SUBMITTED: pending_task_reject_reason.pop(m.from_user.id,None); bot.reply_to(m, "Это выполнение уже обработано."); return
            reason=get_message_text(m).strip()
            if not reason: bot.reply_to(m, "Нужно написать причину отказа текстом."); return
            task=get_task(int(claim['task_id'])); player=get_player(int(claim['user_id'])); username=player['username'] if player and player['username'] else str(claim['user_id'])
            set_task_claim_status(cid, TASK_STATUS_REJECTED, reviewer_id=m.from_user.id, reject_reason=reason); update_master_seals(m.from_user.id, 1, 'review_reject_task', m.from_user.id, f'claim_id={cid}'); pending_task_reject_reason.pop(m.from_user.id,None)
            text=f"@{username}, Мастера отклонили выполнение задания «{task['title']}». Причина: {reason}"
            try: bot.send_message(int(claim['user_id']), text)
            except Exception: pass
            notify_results(text)
            try: bot.edit_message_text(chat_id=MASTERS_CHAT_ID, message_id=claim['masters_control_message_id'], text=f"<b>Выполнение задания #{cid}</b>\nИгрок: <code>{claim['user_id']}</code>\nЗадание: <b>{task['title']}</b>\nСтатус: <b>rejected</b>\nПричина: {reason}\nМастер: <code>{m.from_user.id}</code>")
            except Exception: pass
            return
        # artwork score numeric reply
        sub=get_submission_by_control_message(m.reply_to_message.message_id)
        if sub is not None and sub['id'] in pending_score_input:
            if pending_score_input[sub['id']]!=m.from_user.id: return
            raw=get_message_text(m)
            if not raw.isdigit(): bot.reply_to(m, "Нужно отправить число от 20 до 1000 с шагом 10."); return
            score=int(raw)
            if score<20 or score>1000 or score%10!=0: bot.reply_to(m, "Нужно отправить число от 20 до 1000 с шагом 10."); return
            if sub['status']!='awaiting_score': pending_score_input.pop(sub['id'],None); bot.reply_to(m, "Эта заявка уже не ждёт оценку."); return
            up=update_player_xp(int(sub['user_id']), score, int(sub['id']), 'masters_review', m.from_user.id); set_submission_status(int(sub['id']), 'approved', reviewer_id=m.from_user.id, awarded_xp=score); update_master_seals(m.from_user.id, 1, 'review_approve_work', m.from_user.id, f'submission_id={sub['id']}'); pending_score_input.pop(sub['id'],None)
            txt=f"Поздравляем, @{up['username'] or sub['user_id']}! Мастера оценили вашу работу по достоинству и наградили вас {score}✶"
            try: bot.send_message(int(sub['user_id']), txt)
            except Exception: pass
            notify_results(txt)
            try: bot.edit_message_text(chat_id=MASTERS_CHAT_ID, message_id=sub['masters_control_message_id'], text=f"<b>Заявка #{sub['id']}</b>\nИгрок: <code>{sub['user_id']}</code>\nСтатус: <b>approved</b>\nНаграда: <b>{score}✶</b>\nМастер: <code>{m.from_user.id}</code>")
            except Exception: pass
            return

# callbacks
@bot.callback_query_handler(func=lambda c: c.data.startswith("profile:"))
def cb_profile(c):
    if c.data=="profile:inventory":
        bot.answer_callback_query(c.id); text=render_inventory_text(c.from_user.id); kb=inventory_keyboard(c.from_user.id); bot.send_message(c.message.chat.id,text,reply_markup=kb); return
    if c.data=="profile:edit_about":
        bot.answer_callback_query(c.id); pending_about_edit.add(c.from_user.id); send_prompt("about", c.from_user.id, "Отправьте новым сообщением текст, которым хотите заменить описание о себе."); return

@bot.callback_query_handler(func=lambda c: c.data.startswith("inventory:"))
def cb_inventory(c):
    if get_player(c.from_user.id) is None: bot.answer_callback_query(c.id, "Сначала создайте профиль."); return
    parts=c.data.split(":")
    if parts[1]=="back":
        text=render_inventory_text(c.from_user.id); kb=inventory_keyboard(c.from_user.id)
        try: bot.edit_message_text(text,c.message.chat.id,c.message.message_id,reply_markup=kb)
        except Exception: bot.send_message(c.message.chat.id,text,reply_markup=kb)
        bot.answer_callback_query(c.id); return
    if parts[1]=="view":
        try: text,kb=render_inventory_item(c.from_user.id,int(parts[2]))
        except RuntimeError as exc: bot.answer_callback_query(c.id, str(exc), show_alert=True); return
        try: bot.edit_message_text(text,c.message.chat.id,c.message.message_id,reply_markup=kb)
        except Exception: bot.send_message(c.message.chat.id,text,reply_markup=kb)
        bot.answer_callback_query(c.id); return

@bot.callback_query_handler(func=lambda c: c.data.startswith("dice:"))
def cb_dice(c):
    if needs_verification(c.from_user.id):
        bot.answer_callback_query(c.id, "Сначала пройдите верификацию.")
        return
    if get_player(c.from_user.id) is None:
        bot.answer_callback_query(c.id, "Сначала создайте профиль.")
        return
    parts=c.data.split(":")
    action=parts[1]
    duel_id=int(parts[2])
    duel=get_dice_duel(duel_id)
    if duel is None:
        bot.answer_callback_query(c.id, "Дуэль не найдена.")
        return
    if action=="cancel":
        creator_id=int(parts[3])
        if c.from_user.id not in (creator_id, ADMIN_ID):
            bot.answer_callback_query(c.id, "Отменить вызов может только создатель.")
            return
        if duel["status"]!=DICE_STATUS_OPEN:
            bot.answer_callback_query(c.id, "Дуэль уже недоступна.")
            return
        cancel_dice_duel_and_refund(duel_id, DICE_STATUS_CANCELLED, f"<b>Стол костей #{duel_id}</b>\nВызов отменён. {user_label_by_id(int(duel['creator_id']))} получает обратно <b>{duel['stake_lux']}❂</b>.")
        bot.answer_callback_query(c.id, "Вызов отменён.")
        return
    if action=="accept":
        if duel["status"]!=DICE_STATUS_OPEN:
            bot.answer_callback_query(c.id, "Дуэль уже недоступна.")
            return
        if int(duel["creator_id"])==c.from_user.id:
            bot.answer_callback_query(c.id, "Нельзя принимать собственный вызов.")
            return
        if get_open_dice_duel_for_user(c.from_user.id) is not None:
            bot.answer_callback_query(c.id, "У вас уже есть активная дуэль.")
            return
        try:
            resolve_dice_duel(duel_id, c.from_user.id)
        except RuntimeError as exc:
            bot.answer_callback_query(c.id, str(exc), show_alert=True)
            return
        except Exception:
            logging.exception("[DICE] failed to resolve duel")
            bot.answer_callback_query(c.id, "Не удалось завершить дуэль.", show_alert=True)
            return
        bot.answer_callback_query(c.id, "Кости брошены.")
        return

@bot.callback_query_handler(func=lambda c: c.data.startswith("review:"))
def cb_review(c):
    if not is_master(c.from_user.id): bot.answer_callback_query(c.id, "Недостаточно прав."); return
    action, sid = c.data.split(":")[1], int(c.data.split(":")[2]); sub=get_submission(sid)
    if sub is None: bot.answer_callback_query(c.id, "Заявка не найдена."); return
    if sub['status'] not in ('pending','awaiting_score'): bot.answer_callback_query(c.id, f"Уже обработано: {sub['status']}"); return
    if action=='score':
        set_submission_status(sid,'awaiting_score', reviewer_id=c.from_user.id); pending_score_input[sid]=c.from_user.id
        try: bot.edit_message_text(chat_id=MASTERS_CHAT_ID,message_id=sub['masters_control_message_id'],text=f"<b>Заявка #{sid}</b>\nИгрок: <code>{sub['user_id']}</code>\nСтатус: <b>awaiting_score</b>\nМастер: <code>{c.from_user.id}</code>\nReply числом от 20 до 1000 с шагом 10.", reply_markup=review_keyboard(sid))
        except Exception: pass
        bot.answer_callback_query(c.id,"Отправьте reply числом от 20 до 1000."); return
    if action=='reject':
        pending_art_reject_reason[c.from_user.id]=sid
        bot.answer_callback_query(c.id)
        try:
            bot.send_message(MASTERS_CHAT_ID, f"Мастер <code>{c.from_user.id}</code>, ответьте reply на это сообщение причиной отказа для заявки #{sid}.", reply_to_message_id=sub['masters_control_message_id'])
        except Exception:
            bot.send_message(MASTERS_CHAT_ID, f"Мастер <code>{c.from_user.id}</code>, ответьте причиной отказа для заявки #{sid}.")
        return

@bot.callback_query_handler(func=lambda c: c.data.startswith("shop:"))
def cb_shop(c):
    if get_player(c.from_user.id) is None: bot.answer_callback_query(c.id, "Сначала создайте профиль."); return
    parts=c.data.split(":"); action=parts[1]
    if action=='page':
        text,kb=render_shop_page(int(parts[2]))
        try: bot.edit_message_text(text,c.message.chat.id,c.message.message_id,reply_markup=kb)
        except Exception: bot.send_message(c.message.chat.id,text,reply_markup=kb)
        bot.answer_callback_query(c.id); return
    if action=='view':
        item=get_shop_item(int(parts[2])); page=int(parts[3])
        if item is None or int(item['is_active'])!=1: bot.answer_callback_query(c.id, "Предмет недоступен."); return
        text,kb=render_shop_item(item,page)
        try: bot.edit_message_text(text,c.message.chat.id,c.message.message_id,reply_markup=kb)
        except Exception: bot.send_message(c.message.chat.id,text,reply_markup=kb)
        bot.answer_callback_query(c.id); return
    if action=='buy':
        item_id=int(parts[2]); page=int(parts[3])
        try: player,item=buy_shop_item(c.from_user.id,item_id)
        except RuntimeError as exc: bot.answer_callback_query(c.id, str(exc), show_alert=True); return
        bot.answer_callback_query(c.id, "Покупка совершена.")
        purchase_text=f"Вы приобрели предмет: {item['name']}\nСписано: {item['price_lux']}❂\nНовый баланс: {player['lux']}❂"
        if item['download_url']: purchase_text += f"\n\nСсылка на скачивание: {item['download_url']}"
        bot.send_message(c.from_user.id,purchase_text)
        try: bot.delete_message(c.message.chat.id,c.message.message_id)
        except Exception: pass
        return

@bot.callback_query_handler(func=lambda c: c.data.startswith("tasksmenu:"))
def cb_tasksmenu(c):
    action=c.data.split(":")[1]
    if action=='home':
        try: bot.edit_message_text(tasks_intro_text(), c.message.chat.id, c.message.message_id, reply_markup=tasks_intro_keyboard())
        except Exception: bot.send_message(c.message.chat.id, tasks_intro_text(), reply_markup=tasks_intro_keyboard())
        bot.answer_callback_query(c.id); return
    if action=='list':
        text,kb=render_tasks_page(1)
        try: bot.edit_message_text(text,c.message.chat.id,c.message.message_id,reply_markup=kb)
        except Exception: bot.send_message(c.message.chat.id,text,reply_markup=kb)
        bot.answer_callback_query(c.id); return
    if action=='history':
        text,kb=render_task_history(c.from_user.id)
        try: bot.edit_message_text(text,c.message.chat.id,c.message.message_id,reply_markup=kb)
        except Exception: bot.send_message(c.message.chat.id,text,reply_markup=kb)
        bot.answer_callback_query(c.id); return

@bot.callback_query_handler(func=lambda c: c.data.startswith("tasks:"))
def cb_tasks(c):
    if get_player(c.from_user.id) is None: bot.answer_callback_query(c.id, "Сначала создайте профиль."); return
    parts=c.data.split(":"); action=parts[1]
    if action=='page':
        text,kb=render_tasks_page(int(parts[2]))
        try: bot.edit_message_text(text,c.message.chat.id,c.message.message_id,reply_markup=kb)
        except Exception: bot.send_message(c.message.chat.id,text,reply_markup=kb)
        bot.answer_callback_query(c.id); return
    if action=='view':
        task_id=int(parts[2]); page=int(parts[3]); task=get_task(task_id)
        if task is None or int(task['is_active'])!=1: bot.answer_callback_query(c.id, "Задание недоступно."); return
        text,kb=render_task(task,page,c.from_user.id)
        try: bot.edit_message_text(text,c.message.chat.id,c.message.message_id,reply_markup=kb)
        except Exception: bot.send_message(c.message.chat.id,text,reply_markup=kb)
        bot.answer_callback_query(c.id); return
    if action=='claim':
        task_id=int(parts[2]); page=int(parts[3])
        try: create_task_claim(task_id,c.from_user.id)
        except RuntimeError as exc: bot.answer_callback_query(c.id, str(exc), show_alert=True); return
        task=get_task(task_id); text,kb=render_task(task,page,c.from_user.id)
        try: bot.edit_message_text(text,c.message.chat.id,c.message.message_id,reply_markup=kb)
        except Exception: bot.send_message(c.message.chat.id,text,reply_markup=kb)
        bot.answer_callback_query(c.id, "Задание взято."); return
    if action=='submit':
        claim_id=int(parts[2]); claim=get_task_claim(claim_id)
        if claim is None or int(claim['user_id'])!=c.from_user.id or claim['status']!=TASK_STATUS_ACTIVE: bot.answer_callback_query(c.id,"Это выполнение уже недоступно.", show_alert=True); return
        pending_task_submit[c.from_user.id]=claim_id; send_prompt("task_submit", c.from_user.id, "Отправьте одним сообщением описание выполнения задания. Оно уйдёт Мастерам на рассмотрение.")
        try: bot.delete_message(c.message.chat.id,c.message.message_id)
        except Exception: pass
        bot.answer_callback_query(c.id); return

@bot.callback_query_handler(func=lambda c: c.data.startswith("taskreview:"))
def cb_taskreview(c):
    if not is_master(c.from_user.id): bot.answer_callback_query(c.id, "Недостаточно прав."); return
    parts=c.data.split(":"); action=parts[1]; cid=int(parts[2]); claim=get_task_claim(cid)
    if claim is None: bot.answer_callback_query(c.id, "Заявка на задание не найдена."); return
    if claim['status']!=TASK_STATUS_SUBMITTED: bot.answer_callback_query(c.id, f"Уже обработано: {claim['status']}"); return
    task=get_task(int(claim['task_id'])); player=get_player(int(claim['user_id'])); username=player['username'] if player and player['username'] else str(claim['user_id'])
    if action=='approve':
        set_task_claim_status(cid,TASK_STATUS_APPROVED, reviewer_id=c.from_user.id)
        update_master_seals(c.from_user.id, 1, 'review_approve_task', c.from_user.id, f'claim_id={cid}')
        up=update_player_lux(int(claim['user_id']), int(task['reward_lux']), 'task_reward', c.from_user.id, f"task_id={task['id']}")
        txt=f"Поздравляем, @{username}! Мастера подтвердили выполнение задания «{task['title']}» и вы получили {task['reward_lux']}❂\nВаш новый баланс: {up['lux']}❂"
        try: bot.send_message(int(claim['user_id']), txt)
        except Exception: pass
        notify_results(txt)
        try: bot.edit_message_text(chat_id=MASTERS_CHAT_ID, message_id=claim['masters_control_message_id'], text=f"<b>Выполнение задания #{cid}</b>\nИгрок: <code>{claim['user_id']}</code>\nЗадание: <b>{task['title']}</b>\nСтатус: <b>approved</b>\nНаграда: <b>{task['reward_lux']}❂</b>\nМастер: <code>{c.from_user.id}</code>")
        except Exception: pass
        bot.answer_callback_query(c.id, "Задание подтверждено."); return
    if action=='reject':
        pending_task_reject_reason[c.from_user.id]=cid
        bot.answer_callback_query(c.id)
        try:
            bot.send_message(MASTERS_CHAT_ID, f"Мастер <code>{c.from_user.id}</code>, ответьте reply на это сообщение причиной отказа для выполнения задания #{cid}.", reply_to_message_id=claim['masters_control_message_id'])
        except Exception:
            bot.send_message(MASTERS_CHAT_ID, f"Мастер <code>{c.from_user.id}</code>, ответьте причиной отказа для выполнения задания #{cid}.")
        return

@bot.callback_query_handler(func=lambda c: c.data.startswith("adminreview:"))
def cb_adminreview(c):
    if not is_admin_raw(c.from_user.id):
        bot.answer_callback_query(c.id, 'Недостаточно прав.')
        return
    _, kind, action, raw_id = c.data.split(':', 3)
    sub_id=int(raw_id)
    if kind=='mtask':
        sub=get_master_task_submission(sub_id)
        if sub is None or sub['status']!='pending':
            bot.answer_callback_query(c.id, 'Заявка уже обработана.')
            return
        if action=='approve':
            task=create_task(sub['title'], sub['description'], int(sub['reward_lux']))
            execute("UPDATE master_task_submissions SET status='approved', reviewed_at=? WHERE id=?", (now_ts(), sub_id))
            update_master_seals(int(sub['creator_id']), 100, 'master_task_approved', ADMIN_ID, f'task_id={task["id"]}')
            send_private_if_possible(int(sub['creator_id']), f'Админ одобрил ваше задание #{sub_id}. В список игроков добавлено: #{task["id"]} {task["title"]}. Награда: 10◈')
            bot.answer_callback_query(c.id, 'Одобрено.')
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            return
        execute("UPDATE master_task_submissions SET status='rejected', reviewed_at=? WHERE id=?", (now_ts(), sub_id))
        send_private_if_possible(int(sub['creator_id']), f'Админ отклонил ваше задание #{sub_id}.')
        bot.answer_callback_query(c.id, 'Отклонено.')
        try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        except Exception: pass
        return
    if kind=='mevent':
        sub=get_master_event_submission(sub_id)
        if sub is None or sub['status']!='pending':
            bot.answer_callback_query(c.id, 'Заявка уже обработана.')
            return
        if action=='approve':
            event=create_master_event(int(sub['creator_id']), sub['title'], sub['description'], int(sub['reward_lux']))
            msg=create_event_post(int(event['id']))
            execute("UPDATE master_event_submissions SET status='approved', reviewed_at=?, event_id=? WHERE id=?", (now_ts(), event['id'], sub_id))
            update_master_seals(int(sub['creator_id']), 500, 'master_event_approved', ADMIN_ID, f'event_id={event["id"]}')
            send_private_if_possible(int(sub['creator_id']), f'Админ одобрил ваше событие #{sub_id}. Опубликовано событие #{event["id"]}. Награда: 50◈')
            bot.answer_callback_query(c.id, 'Одобрено.')
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            return
        execute("UPDATE master_event_submissions SET status='rejected', reviewed_at=? WHERE id=?", (now_ts(), sub_id))
        send_private_if_possible(int(sub['creator_id']), f'Админ отклонил ваше событие #{sub_id}.')
        bot.answer_callback_query(c.id, 'Отклонено.')
        try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        except Exception: pass
        return
    if kind=='mitem':
        sub=get_master_item_submission(sub_id)
        if sub is None or sub['status']!='pending':
            bot.answer_callback_query(c.id, 'Заявка уже обработана.')
            return
        if action=='approve':
            item=create_shop_item(sub['name'], sub['description'], int(sub['price_lux']), sub['rarity'], sub['download_url'] or None)
            execute("UPDATE master_item_submissions SET status='approved', reviewed_at=?, item_id=? WHERE id=?", (now_ts(), item['id'], sub_id))
            reward=MASTER_ITEM_REWARD_TENTHS.get(sub['rarity'], 20)
            update_master_seals(int(sub['creator_id']), reward, 'master_item_approved', ADMIN_ID, f'item_id={item["id"]}')
            send_private_if_possible(int(sub['creator_id']), f'Админ одобрил ваш предмет #{sub_id}. Он добавлен в лавку как #{item["id"]}. Награда: {format_seals_tenths(reward)}')
            bot.answer_callback_query(c.id, 'Одобрено.')
            try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
            except Exception: pass
            return
        execute("UPDATE master_item_submissions SET status='rejected', reviewed_at=? WHERE id=?", (now_ts(), sub_id))
        send_private_if_possible(int(sub['creator_id']), f'Админ отклонил ваш предмет #{sub_id}.')
        bot.answer_callback_query(c.id, 'Отклонено.')
        try: bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        except Exception: pass
        return

def main():
    init_db()
    threading.Thread(target=leaders_scheduler_loop, daemon=True).start()
    threading.Thread(target=expire_dice_duels_loop, daemon=True).start()
    print("=== RUNNING STAGE 9 ===")
    logging.info("Stage 9 bot is running")
    bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60, allowed_updates=["message", "callback_query", "message_reaction"])

if __name__=='__main__':
    main()
