"""Бот вакансий: читает каналы, фильтрует ИИ, раскладывает по темам группы.

Каналы читаются двумя способами:
  без аккаунта, через публичную веб версию t.me/s/<канал> (нужен только BOT_TOKEN);
  через аккаунт Telegram (TG_API_ID, TG_API_HASH, сессия), тогда доступны и закрытые чаты.

Запуск:
  python main.py --setup   найти группу (или создать её через аккаунт) и сделать темы (один раз)
  python main.py --once    одна проверка каналов (так работает GitHub Actions)
  python main.py           проверять по кругу каждые loop_minutes минут
"""
import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import time
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telethon import TelegramClient, errors, functions, types
from telethon.sessions import StringSession

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / "keys.env"); load_dotenv(BASE / ".env")
CFG = yaml.safe_load((BASE / "config.yaml").read_text("utf-8"))
STATE_PATH = BASE / "state.json"
SESSION_FILE = BASE / "session.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vacancy-bot")
logging.getLogger("telethon").setLevel(logging.WARNING)


# ---------------- состояние ----------------

ENC_PATH = BASE / "state.enc"


def _fernet():
    key = (os.getenv("STATE_KEY") or "").strip()
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def load_state():
    f = _fernet()
    if f and ENC_PATH.exists():
        return json.loads(f.decrypt(ENC_PATH.read_bytes()).decode("utf-8"))
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text("utf-8"))
    return {}


def save_state(state):
    state["seen"] = state.get("seen", [])[-3000:]
    state["pending"] = state.get("pending", [])[-150:]
    data = json.dumps(state, ensure_ascii=False, indent=1)
    f = _fernet()
    if f:
        ENC_PATH.write_bytes(f.encrypt(data.encode("utf-8")))
    else:
        STATE_PATH.write_text(data, "utf-8")


def env(name):
    val = (os.getenv(name) or "").strip()
    if not val:
        raise SystemExit(f"Не заполнено {name} в keys.env (или в секретах GitHub)")
    return val


# ---------------- Telegram ----------------

def make_client():
    session = (os.getenv("TG_SESSION") or "").strip()
    if not session and SESSION_FILE.exists():
        session = SESSION_FILE.read_text().strip()
    if not session or not os.getenv("TG_API_ID") or not os.getenv("TG_API_HASH"):
        return None
    return TelegramClient(StringSession(session), int(env("TG_API_ID")), env("TG_API_HASH"))


class Bot:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, _timeout=30, **params):
        for _ in range(5):
            r = requests.post(self.base + method, json=params, timeout=_timeout)
            data = r.json()
            if data.get("ok"):
                return data["result"]
            if data.get("error_code") == 429:
                time.sleep(data.get("parameters", {}).get("retry_after", 5) + 1)
                continue
            raise RuntimeError(f"Bot API {method}: {data.get('description')}")
        raise RuntimeError(f"Bot API {method}: слишком много повторов")


def find_group(bot, state):
    """Ищет группу, куда пользователь добавил бота, по последним событиям бота."""
    updates = bot.call("getUpdates", allowed_updates=["message", "my_chat_member"])
    seen = {(u.get("my_chat_member") or u.get("message") or {}).get("chat", {}).get("type") for u in updates}
    log.info("Бот видит событий: %d, типы чатов: %s", len(updates), ", ".join(sorted(t for t in seen if t)) or "нет")
    for upd in reversed(updates):
        obj = upd.get("my_chat_member") or upd.get("message") or {}
        chat = obj.get("chat") or {}
        if chat.get("type") == "supergroup":
            state["chat_id"] = chat["id"]
            log.info("Нашёл группу: %s", chat.get("title"))
            return True
        if chat.get("type") == "group":
            raise SystemExit("Группа ещё обычная. Включи в её настройках «Темы», "
                             "потом напиши в неё любое сообщение и запусти снова")
    return False


async def setup(client, bot, state):
    me = bot.call("getMe")
    if not state.get("chat_id") and client is None:
        if not find_group(bot, state):
            raise SystemExit(
                f"Не вижу группу. Создай группу, включи в настройках «Темы», добавь @{me['username']} "
                "админом с правом управлять темами, отправь в группу команду /start@" + me["username"] + " и запусти снова")
    if not state.get("chat_id"):
        res = await client(functions.channels.CreateChannelRequest(
            title=CFG.get("group_title", "Вакансии"),
            about="Вакансии, отобранные ИИ",
            megagroup=True, forum=True))
        chat = res.chats[0]
        state["chat_id"] = int(f"-100{chat.id}")
        rights = types.ChatAdminRights(
            change_info=True, post_messages=True, edit_messages=True, delete_messages=True,
            invite_users=True, pin_messages=True, manage_topics=True)
        await client(functions.channels.EditAdminRequest(
            channel=chat, user_id="@" + me["username"], admin_rights=rights, rank="bot"))
        save_state(state)
        log.info("Группа создана, бот @%s назначен админом", me["username"])
    topics = state.setdefault("topics", {})
    wanted = dict(CFG["sections"])
    wanted["applied"] = CFG.get("applied_topic", "Мои отклики")
    for key, title in wanted.items():
        if key not in topics:
            try:
                t = bot.call("createForumTopic", chat_id=state["chat_id"], name=title)
            except RuntimeError as err:
                save_state(state)
                if "rights" in str(err) or "admin" in str(err).lower():
                    raise SystemExit(f"Бот в группе, но не админ или без права «Управление темами». "
                                     f"Сделай @{me['username']} админом с этим правом и запусти снова")
                if "forum" in str(err).lower() or "topic" in str(err).lower():
                    raise SystemExit("В группе не включены «Темы». Включи их в настройках группы и запусти снова")
                raise
            topics[key] = t["message_thread_id"]
            log.info("Тема создана: %s", title)
    save_state(state)


INVITE_RE = re.compile(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([\w-]+)")


async def resolve(client, link, state):
    peers = state.setdefault("peers", {})
    p = peers.get(link)
    if not p:
        m = INVITE_RE.search(link)
        if is_title(link):
            ent = None
            want = link.strip().lower()
            async for d in client.iter_dialogs():
                if (d.name or "").strip().lower() == want:
                    ent = d.entity
                    break
            if ent is None:
                raise ValueError("нет чата с таким названием среди твоих чатов")
        elif m:
            try:
                upd = await client(functions.messages.ImportChatInviteRequest(m.group(1)))
                ent = upd.chats[0]
            except errors.UserAlreadyParticipantError:
                ent = (await client(functions.messages.CheckChatInviteRequest(m.group(1)))).chat
        else:
            uname = re.sub(r"^(https?://)?((t|telegram)\.me/)?@?", "", link.strip()).split("/")[0]
            ent = await client.get_entity(uname)
        p = {"id": ent.id, "title": getattr(ent, "title", str(ent.id)),
             "username": getattr(ent, "username", None),
             "hash": getattr(ent, "access_hash", None),
             "kind": "channel" if isinstance(ent, types.Channel) else "chat"}
        peers[link] = p
    if p["kind"] == "channel":
        return types.InputPeerChannel(p["id"], p["hash"]), p
    return types.InputPeerChat(p["id"]), p


WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}


def public_username(link):
    if INVITE_RE.search(link):
        return None
    name = re.sub(r"^(https?://)?((t|telegram)\.me/)?(s/)?@?", "", link.strip()).split("/")[0]
    return name if re.fullmatch(r"[A-Za-z0-9_]{4,}", name or "") else None


def is_title(link):
    """Название чата вместо ссылки, например «МИР КРЕАТОРОВ»."""
    return not INVITE_RE.search(link) and public_username(link) is None


def parse_web_page(page):
    """Разбирает t.me/s/<канал>: возвращает название и список (id, текст), старые первыми."""
    soup = BeautifulSoup(page, "html.parser")
    title_el = soup.select_one(".tgme_channel_info_header_title")
    title = title_el.get_text(" ", strip=True) if title_el else None
    posts = []
    for box in soup.select("div.tgme_widget_message[data-post]"):
        try:
            msg_id = int(box["data-post"].rsplit("/", 1)[1])
        except (ValueError, IndexError):
            continue
        texts = box.select(".tgme_widget_message_text")
        if not texts:
            continue
        el = texts[-1]
        for br in el.find_all("br"):
            br.replace_with("\n")
        for a in el.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and a.get_text(strip=True) not in href:
                a.append(f" ({href})")
        t = box.select_one(".tgme_widget_message_date time[datetime]")
        posts.append((msg_id, el.get_text().strip(), t["datetime"] if t else None))
    posts.sort()
    return title, posts


def fetch_web(username, last):
    """Новые посты публичного канала без аккаунта, с подгрузкой страниц назад."""
    url = f"https://t.me/s/{username}"
    r = requests.get(url, headers=WEB_HEADERS, timeout=30)
    r.raise_for_status()
    title, posts = parse_web_page(r.text)
    if not posts and "tgme_channel_info" not in r.text:
        raise RuntimeError("канал не открыт для веб просмотра (закрытый или это группа)")
    pages = 1
    while last is not None and posts and posts[0][0] > last + 1 and pages < 5:
        r = requests.get(url, params={"before": posts[0][0]}, headers=WEB_HEADERS, timeout=30)
        if r.status_code != 200:
            break
        _, older = parse_web_page(r.text)
        older = [x for x in older if x[0] < posts[0][0]]
        if not older:
            break
        posts = older + posts
        pages += 1
    if last is not None:
        posts = [x for x in posts if x[0] > last]
    return title or username, posts


def parse_portfolio_page(page):
    """Посты канала портфолио: (id, тип медиа, подпись), включая посты без текста."""
    soup = BeautifulSoup(page, "html.parser")
    out = []
    for box in soup.select("div.tgme_widget_message[data-post]"):
        try:
            msg_id = int(box["data-post"].rsplit("/", 1)[1])
        except (ValueError, IndexError):
            continue
        if box.select_one(".tgme_widget_message_video_player, .tgme_widget_message_roundvideo_player"):
            kind = "видео"
        elif box.select_one(".tgme_widget_message_photo_wrap"):
            kind = "фото"
        elif box.select_one(".tgme_widget_message_document"):
            kind = "файл"
        else:
            kind = "текст"
        el = box.select_one(".tgme_widget_message_text")
        text = el.get_text(" ", strip=True) if el else ""
        out.append((msg_id, kind, text))
    return out


def load_portfolio(state):
    """Список работ из ТГК портфолио, обновляется раз в сутки."""
    items = _load_portfolio(state)
    load_portfolio.cache = items
    return items


load_portfolio.cache = []


def _load_portfolio(state):
    uname = public_username(CFG.get("portfolio_channel") or "")
    if not uname:
        return []
    cache = state.get("portfolio") or {}
    if cache.get("channel") == uname and time.time() - cache.get("ts", 0) < 86400:
        return cache.get("items", [])
    posts, before = {}, None
    try:
        for _ in range(8):
            r = requests.get(f"https://t.me/s/{uname}", params={"before": before} if before else None,
                             headers=WEB_HEADERS, timeout=30)
            if r.status_code != 200:
                break
            page = parse_portfolio_page(r.text)
            new = [x for x in page if x[0] not in posts]
            if not new:
                break
            for x in new:
                posts[x[0]] = x
            before = min(x[0] for x in page)
    except requests.RequestException as e:
        log.warning("Портфолио %s: не удалось прочитать (%s)", uname, e)
        return cache.get("items", [])
    items = [{"url": f"https://t.me/{uname}/{i}", "kind": k, "text": t[:160]}
             for i, k, t in sorted(posts.values(), reverse=True) if k != "текст" or len(t) > 30]
    items = items[:int(CFG.get("portfolio_max_items", 80))]
    state["portfolio"] = {"channel": uname, "ts": time.time(), "items": items}
    log.info("Портфолио: работ найдено %d", len(items))
    return items


def msg_url(p, msg_id):
    if p.get("username"):
        return f"https://t.me/{p['username']}/{msg_id}"
    if p["kind"] == "channel":
        return f"https://t.me/c/{p['id']}/{msg_id}"
    return None


def fingerprint(text):
    norm = re.sub(r"[^\w]+", "", text.lower())[:400]
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def signature(text, k=48):
    """Отпечаток смысла текста: ловит одну вакансию, перепощенную с другой шапкой или хэштегами."""
    words = re.findall(r"\w+", re.sub(r"https?://\S+|#\w+|@\w+", " ", text.lower()))
    grams = {" ".join(words[i:i + 3]) for i in range(max(len(words) - 2, 1))}
    return sorted(int(hashlib.md5(g.encode()).hexdigest()[:8], 16) for g in grams)[:k]


def similar(a, b, k=48):
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    union = sorted(sa | sb)[:k]
    return sum(1 for x in union if x in sa and x in sb) / len(union)


def is_duplicate(text, state):
    """True, если такая вакансия уже была (точь-в-точь или почти, в том числе из другого канала)."""
    seen = state.setdefault("seen", [])
    sigs = state.setdefault("sigs", [])
    fp = fingerprint(text)
    if fp in seen:
        return True
    sig = signature(text)
    if any(similar(sig, old) >= 0.5 for old in sigs):
        seen.append(fp)
        return True
    seen.append(fp)
    sigs.append(sig)
    state["sigs"] = sigs[-800:]
    return False


async def collect(client, state):
    """Забирает новые сообщения из всех каналов. Возвращает список вакансий-кандидатов."""
    last_ids = state.setdefault("last_ids", {})
    stopwords = [w.lower() for w in CFG.get("stopwords") or []]
    items = []
    for link in CFG.get("channels") or []:
        uname = public_username(link)
        if client is None or (CFG.get("prefer_web", True) and uname):
            if not uname:
                log.error("%s: закрытые группы читаются только через аккаунт Telegram (нужен api_id)", link)
                continue
            last = last_ids.get(link)
            try:
                title, posts = fetch_web(uname, last)
            except Exception as e:
                log.error("%s: не удалось прочитать (%s)", link, e)
                continue
            if last is None:
                posts = posts[-int(CFG.get("first_run_backfill", 10)):]
            for msg_id, text, posted in posts:
                last_ids[link] = max(last_ids.get(link) or 0, msg_id)
                if len(text) < 40 or any(w in text.lower() for w in stopwords):
                    continue
                if is_duplicate(text, state):
                    continue
                items.append({"src": title, "url": f"https://t.me/{uname}/{msg_id}", "date": posted,
                              "author": None, "text": text[:3500], "fp": fingerprint(text)})
            log.info("%s: новых постов %d", title, len(posts))
            continue
        try:
            peer, p = await resolve(client, link, state)
            last = last_ids.get(link)
            if last is None:
                msgs = await client.get_messages(peer, limit=int(CFG.get("first_run_backfill", 10)))
            else:
                msgs = await client.get_messages(peer, min_id=last, limit=200)
        except errors.FloodWaitError as e:
            log.warning("%s: Telegram просит подождать %s сек, пропускаю", link, e.seconds)
            continue
        except Exception as e:
            log.error("%s: не удалось прочитать (%s)", link, e)
            continue
        for msg in reversed(msgs):
            last_ids[link] = max(last_ids.get(link) or 0, msg.id)
            text = (msg.message or "").strip()
            if len(text) < 40:
                continue
            low = text.lower()
            if any(w in low for w in stopwords):
                continue
            if is_duplicate(text, state):
                continue
            sender = getattr(msg, "sender", None)
            author = getattr(sender, "username", None) if isinstance(sender, types.User) else None
            items.append({"src": p["title"], "url": msg_url(p, msg.id),
                          "date": msg.date.isoformat() if msg.date else None,
                          "author": author, "text": text[:3500], "fp": fingerprint(text)})
        log.info("%s: сообщений получено %d", p["title"], len(msgs))
    return items


# ---------------- ИИ ----------------

def system_prompt(portfolio=()):
    works = "\n".join(f"- {w['url']} [{w['kind']}] {w['text'] or 'без подписи'}" for w in portfolio)
    portfolio_block = (f"""
ПОРТФОЛИО (мои работы в Телеграм канале, ссылка = пост с работой):
{works}
Выбери 1-3 самые подходящие под задачу работы из этого списка и верни их ссылки в поле works.
Бери ссылки только из списка, ничего не придумывай. Эти работы бот сам перешлёт сразу после отклика.
Поэтому в тексте reply ссылок на работы НЕ пиши, а коротко и естественно скажи, что примеры ниже
(например "Скинул пару похожих работ ниже" или "Примеры ниже"), формулировку меняй от отклика к отклику.
Если подходящих работ нет, works оставь пустым, а в reply дай ссылку на канал:
https://t.me/{public_username(CFG.get('portfolio_channel') or '')}
""" if portfolio else "")
    sections = "\n".join(f"- {k}: {v}" for k, v in CFG["sections"].items() if k != "doubt")
    return f"""Ты фильтруешь сообщения из Телеграм каналов с вакансиями для фрилансера.

ПРОФИЛЬ ИСПОЛНИТЕЛЯ:
{CFG['profile']}

ЧТО НЕ ПОДХОДИТ:
{CFG.get('not_fit', '')}

МИНИМАЛЬНАЯ ОПЛАТА: {CFG.get('min_rate') or 'не задана'}

РАЗДЕЛЫ:
{sections}

ПРАВИЛА ОТКЛИКА:
{CFG.get('reply_rules', '')}
{portfolio_block}
Для каждого сообщения верни объект:
- i: номер сообщения
- is_vacancy: true, если это предложение работы или заказа (а не реклама, резюме, новость)
- fit: "yes" (точно подходит), "maybe" (пограничное, мало данных, оплата под вопросом), "no"
- section: ключ раздела из списка выше
- summary: суть задачи в 1-2 предложениях на русском
- pay: оплата, как указана в тексте, или ""
- reason: почему подходит или нет, одно короткое предложение на русском
- contact: куда писать (@username или ссылка) из текста, или ""
- reply: готовый отклик, только если fit "yes" или "maybe", иначе ""
- urgency: "high" (нужно сегодня, завтра, в ближайшие дни, слова срочно, ASAP, горит),
  "normal" (обычные сроки или сроки не указаны), "low" (долгий проект, без спешки)
- deadline: срок из текста коротко ("до пятницы", "за 2 дня") или ""
- works: список ссылок на подходящие работы из портфолио (0-3), или []

Ответ строго JSON: {{"results": [ ... ]}}"""


def user_prompt(batch):
    parts = []
    for i, it in enumerate(batch):
        parts.append(f"### Сообщение {i} (источник: {it['src']})\n{it['text']}")
    return "\n\n".join(parts)


_gemini_auto = []


def gemini_auto_models(key):
    if _gemini_auto:
        return _gemini_auto
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"pageSize": 200}, headers={"x-goog-api-key": key}, timeout=30)
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        flash = [n for n in names if "flash" in n and "image" not in n and "tts" not in n
                 and "live" not in n and "audio" not in n]
        flash.sort(key=lambda n: ("preview" in n or "exp" in n, "lite" not in n), reverse=False)
        _gemini_auto.extend(flash[:4])
    except Exception as e:
        log.warning("Не удалось получить список моделей Gemini: %s", e)
    return _gemini_auto


def ask_gemini(system, prompt):
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    models = list(CFG["ai"].get("gemini_models") or [])
    tried_auto = False
    i = 0
    while i < len(models):
        model = models[i]
        i += 1
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3}}
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key}, json=body, timeout=120)
        except requests.RequestException as e:
            log.warning("Gemini %s: %s", model, e)
            continue
        if r.status_code == 200:
            try:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError):
                log.warning("Gemini %s: пустой ответ", model)
                continue
        log.warning("Gemini %s: HTTP %s %s", model, r.status_code, r.text[:200])
        if r.status_code == 404 and not tried_auto:
            tried_auto = True
            models += [m for m in gemini_auto_models(key) if m not in models]
    return None


def ask_groq(system, prompt):
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return None
    for model in CFG["ai"].get("groq_models") or []:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "temperature": 0.3,
                      "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": prompt}]},
                timeout=120)
        except requests.RequestException as e:
            log.warning("Groq %s: %s", model, e)
            continue
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        log.warning("Groq %s: HTTP %s %s", model, r.status_code, r.text[:200])
    return None


def parse_results(raw):
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if isinstance(data, list):
        return data
    return data.get("results") if isinstance(data, dict) else None


def classify(batch, portfolio=()):
    system, prompt = system_prompt(portfolio), user_prompt(batch)
    for ask in (ask_gemini, ask_groq):
        res = parse_results(ask(system, prompt))
        if res is not None:
            return res
    return None


# ---------------- проверка отклика на признаки ИИ ----------------
# Правила взяты из github.com/blader/humanizer (по гайду Википедии "Signs of AI writing")

AI_TELLS = [
    (r"[—–]", "длинное или среднее тире"),
    (r"\bне (просто|только|лишь)\b[^.!?\n]{1,60}?,\s*(а|но)\b", "конструкция «не просто X, а Y»"),
    (r"\bэто не\b[^.!?\n]{1,50}?,\s*это\b", "конструкция «это не X, это Y»"),
    (r"буду рад|с радостью|готов(а)? взяться|с уважением|надеюсь на (ответ|сотрудничество)|"
     r"жду (обратной связи|ответа)|заслуживает", "вежливый шаблон"),
    (r"уникальн|качественн|профессиональн\w* подход|индивидуальн\w* подход|креативн|"
     r"вдохн[уё]|погруз|ключев|идеально подойд|в кратчайшие сроки|под ключ", "пафосные слова"),
    (r"\bменя зовут\b|увидел(а)? (вашу )?вакансию|я опытн", "шаблонное начало"),
    (r"(?i)\b(thrilled|excited to|passionate|delve|leverage|seamless|i hope this)\b", "английские ИИ слова"),
    (r"\*\*|#\w", "разметка или хэштеги"),
    (r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "эмодзи"),
]


def find_tells(text):
    found = []
    for pattern, name in AI_TELLS:
        if re.search(pattern, text, re.I) and name not in found:
            found.append(name)
    return found


def polish(text):
    """Последняя страховка: убирает тире, кавычки-лапки, эмодзи и разметку, не трогая ссылки."""
    text = re.sub(r"\s+[—–]\s+", ", ", text)
    text = re.sub(r"(?<=\w)[—–](?=\w)", "-", text)
    text = text.replace("—", ",").replace("–", "-")
    text = text.replace("“", '"').replace("”", '"').replace("**", "")
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r",\s*,", ",", text)
    return text.strip()


FIX_SYSTEM = """Ты редактор. Перепиши отклик фрилансера на вакансию так, чтобы он звучал как живой человек
в мессенджере, а не как ИИ. Сохрани смысл, ссылки (если есть), подпись и ответ на требования вакансии.
Ничего не выдумывай.
ПРАВИЛА ОТКЛИКА:
{rules}
Ответ строго JSON: {{"reply": "..."}}"""


def fix_reply(item, reply, tells):
    prompt = (f"ВАКАНСИЯ:\n{item['text'][:2000]}\n\nЧЕРНОВИК ОТКЛИКА:\n{reply}\n\n"
              f"НАЙДЕННЫЕ ПРОБЛЕМЫ: {', '.join(tells)}")
    system = FIX_SYSTEM.format(rules=CFG.get("reply_rules", ""))
    for ask in (ask_gemini, ask_groq):
        raw = ask(system, prompt)
        if not raw:
            continue
        try:
            data = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip())
            if isinstance(data, dict) and data.get("reply"):
                return data["reply"]
        except json.JSONDecodeError:
            continue
    return reply


_fix_budget = []


def humanize(item, res):
    reply = res.get("reply") or ""
    if not reply:
        return
    tells = [t for t in find_tells(reply) if t != "длинное или среднее тире"] or []
    if tells and len(_fix_budget) < int(CFG["ai"].get("max_fixes_per_run", 20)):
        _fix_budget.append(1)
        reply = fix_reply(item, reply, tells)
        log.info("Отклик доработан: %s", ", ".join(tells))
    res["reply"] = polish(reply)


# ---------------- карточка ----------------

def contact_url(contact, author):
    c = (contact or "").strip()
    if c.startswith("@"):
        return "https://t.me/" + c[1:]
    if c.startswith("http"):
        return c
    if re.fullmatch(r"(t\.me|telegram\.me)/\S+", c):
        return "https://" + c
    if author:
        return "https://t.me/" + author
    return None


try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(CFG.get("timezone", "Europe/Moscow"))
except Exception:
    from datetime import timezone as _tz, timedelta as _td
    LOCAL_TZ = _tz(_td(hours=3))
from datetime import datetime, timezone


def posted_label(iso):
    """'17.10 20:35, 2 ч назад' в твоём часовом поясе."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    if mins < 60:
        ago = f"{max(mins, 1)} мин назад"
    elif mins < 48 * 60:
        ago = f"{mins // 60} ч назад"
    else:
        ago = f"{mins // 1440} дн назад"
    return f"{dt.astimezone(LOCAL_TZ):%d.%m %H:%M}, {ago}"


URGENCY = {"high": "срочно", "normal": "обычная", "low": "не срочно"}


def build_card(item, res):
    e = html.escape
    section = CFG["sections"].get(res.get("section"), "")
    lines = ["━━━━━━━━━━━━━━━━━━", f"<b>{e(section)}</b> | {e(item['src'])}", ""]
    if res.get("summary"):
        lines.append(e(res["summary"]))
    when = posted_label(item.get("date"))
    if when:
        lines.append(f"<b>Опубликовано:</b> {e(when)}")
    urg = URGENCY.get(res.get("urgency"), "")
    if urg:
        dl = f" ({e(res['deadline'])})" if res.get("deadline") else ""
        lines.append(f"<b>Срочность:</b> {'СРОЧНО' if res.get('urgency') == 'high' else urg}{dl}")
    if res.get("pay"):
        lines.append(f"<b>Оплата:</b> {e(res['pay'])}")
    if res.get("reason"):
        lines.append(f"<i>{e(res['reason'])}</i>")
    if res.get("reply"):
        lines += ["", "<b>Отклик</b> (нажми на текст, чтобы скопировать):",
                  f"<pre>{e(res['reply'][:2000])}</pre>"]
    footer = f"\n\n<b>Работы к этой вакансии ниже: {res['_works_count']}</b>" if res.get("_works_count") else ""
    head = "\n".join(lines)
    room = 3850 - len(head)
    original = item["text"] if len(item["text"]) <= room else item["text"][:max(room, 0)] + "…"
    text = head + (f"\n\n<blockquote expandable>{e(original)}</blockquote>" if room > 200 else "") + footer
    return text, card_buttons(item.get("url"), contact_url(res.get("contact"), item.get("author")),
                              item.get("fp") or fingerprint(item["text"]), None)


def card_buttons(url, contact, fp, applied_at):
    rows, top = [], []
    if url:
        top.append({"text": "Открыть вакансию", "url": url})
    if contact:
        top.append({"text": "Написать автору", "url": contact})
    if top:
        rows.append(top)
    if applied_at:
        rows.append([{"text": f"Откликнулся {applied_at} (снять)", "callback_data": f"u:{fp}"}])
    else:
        rows.append([{"text": "Откликнулся", "callback_data": f"a:{fp}"},
                     {"text": "Не моё", "callback_data": f"h:{fp}"}])
    return rows


def deliver(bot, state, item, res):
    fit = res.get("fit")
    if not res.get("is_vacancy") or fit not in ("yes", "maybe"):
        return False
    humanize(item, res)
    works = pick_works(res)
    forwarding = bool(works) and not _forward_broken and CFG.get("forward_works", True)
    if forwarding:
        res["reply"] = strip_work_links(res.get("reply") or "")
    else:
        uname = public_username(CFG.get("portfolio_channel") or "")
        if works and uname and "t.me/" + uname.lower() + "/" not in (res.get("reply") or "").lower():
            res["reply"] = (res.get("reply") or "").rstrip() + "\n" + "\n".join(
                f"https://t.me/{uname}/{i}" for i in works)
    res["_works_count"] = len(works) if forwarding else 0
    key = res.get("section") if fit == "yes" else "doubt"
    if key not in state.get("topics", {}):
        key = "doubt"
    text, buttons = build_card(item, res)
    params = {"chat_id": state["chat_id"], "message_thread_id": state["topics"][key],
              "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    try:
        msg = bot.call("sendMessage", **params)
    except RuntimeError as err:
        if "button" in str(err).lower() or "url" in str(err).lower():
            params.pop("reply_markup", None)
            msg = bot.call("sendMessage", **params)
        else:
            raise
    sent = state.setdefault("sent_ids", [])
    sent.append(msg["message_id"])
    before = len(sent)
    forward_works(bot, state, works if forwarding else [], params["message_thread_id"], sent)
    state["sent_ids"] = sent[-2000:]
    cards = state.setdefault("cards", {})
    cards[item.get("fp") or fingerprint(item["text"])] = {
        "card": msg["message_id"], "thread": params["message_thread_id"], "works": sent[before:],
        "section": CFG["sections"].get(res.get("section"), ""), "src": item["src"],
        "summary": (res.get("summary") or "")[:300], "pay": res.get("pay") or "",
        "url": item.get("url"), "contact": contact_url(res.get("contact"), item.get("author")),
        "ts": time.time(), "status": None, "entry": None}
    if len(cards) > 1500:
        for old in sorted(cards, key=lambda k: cards[k]["ts"])[:len(cards) - 1500]:
            cards.pop(old)
    return True


# ---------------- учёт откликов (кнопки) ----------------

STATUSES = {"a": "Откликнулся", "r": "Ответили", "w": "В работе", "x": "Отказ", "n": "Без ответа"}


def card_link(state, msg_id, thread):
    return f"https://t.me/c/{str(state['chat_id']).replace('-100', '', 1)}/{thread}/{msg_id}"


def entry_text(state, c):
    e = html.escape
    lines = [f"<b>{e(c['status'])}</b> | {e(c.get('status_at', ''))}",
             f"{e(c['section'])} | {e(c['src'])}", e(c["summary"])]
    if c.get("pay"):
        lines.append(f"Оплата: {e(c['pay'])}")
    links = [f'<a href="{card_link(state, c["card"], c["thread"])}">Карточка</a>']
    if c.get("url"):
        links.append(f'<a href="{e(c["url"])}">Вакансия</a>')
    if c.get("contact"):
        links.append(f'<a href="{e(c["contact"])}">Автор</a>')
    lines.append(" | ".join(links))
    return "\n".join(lines)


def entry_buttons(fp):
    return [[{"text": STATUSES[k], "callback_data": f"s:{fp}:{k}"} for k in ("r", "w")],
            [{"text": STATUSES[k], "callback_data": f"s:{fp}:{k}"} for k in ("x", "n")]]


def now_label():
    return f"{datetime.now(LOCAL_TZ):%d.%m %H:%M}"


def handle_callback(bot, state, cq):
    data = cq.get("data") or ""
    parts = data.split(":")
    fp = parts[1] if len(parts) > 1 else ""
    c = state.get("cards", {}).get(fp)
    note = ""
    try:
        if not c:
            note = "Эта карточка старая, бот её уже не помнит"
        elif parts[0] == "a":
            c["status"], c["status_at"] = STATUSES["a"], now_label()
            bot.call("editMessageReplyMarkup", chat_id=state["chat_id"], message_id=c["card"],
                     reply_markup={"inline_keyboard": card_buttons(c["url"], c["contact"], fp, c["status_at"])})
            topic = state.get("topics", {}).get("applied")
            if topic:
                m = bot.call("sendMessage", chat_id=state["chat_id"], message_thread_id=topic,
                             text=entry_text(state, c), parse_mode="HTML",
                             link_preview_options={"is_disabled": True},
                             reply_markup={"inline_keyboard": entry_buttons(fp)})
                c["entry"] = m["message_id"]
            note = "Отмечено. Запись в «Мои отклики»"
        elif parts[0] == "u":
            c["status"] = None
            bot.call("editMessageReplyMarkup", chat_id=state["chat_id"], message_id=c["card"],
                     reply_markup={"inline_keyboard": card_buttons(c["url"], c["contact"], fp, None)})
            if c.get("entry"):
                bot.call("deleteMessages", chat_id=state["chat_id"], message_ids=[c["entry"]])
                c["entry"] = None
            note = "Отметка снята"
        elif parts[0] == "h":
            ids = [c["card"]] + list(c.get("works") or []) + ([c["entry"]] if c.get("entry") else [])
            bot.call("deleteMessages", chat_id=state["chat_id"], message_ids=ids)
            state["cards"].pop(fp, None)
            note = "Скрыто"
        elif parts[0] == "s" and len(parts) > 2 and parts[2] in STATUSES:
            c["status"], c["status_at"] = STATUSES[parts[2]], now_label()
            if c.get("entry"):
                bot.call("editMessageText", chat_id=state["chat_id"], message_id=c["entry"],
                         text=entry_text(state, c), parse_mode="HTML",
                         link_preview_options={"is_disabled": True},
                         reply_markup={"inline_keyboard": entry_buttons(fp)})
            note = f"Статус: {c['status']}"
    except RuntimeError as err:
        log.warning("Кнопка не сработала: %s", err)
        note = "Не получилось, попробуй ещё раз"
    try:
        bot.call("answerCallbackQuery", callback_query_id=cq["id"], text=note[:190])
    except RuntimeError:
        pass


def handle_updates(bot, state, wait=0):
    """Обрабатывает нажатия кнопок. wait > 0: ждёт нажатий до wait секунд."""
    try:
        updates = bot.call("getUpdates", offset=state.get("upd_offset", 0), timeout=wait,
                           allowed_updates=["callback_query"], _timeout=wait + 20)
    except Exception as err:
        log.warning("Не удалось получить нажатия кнопок: %s", err)
        time.sleep(min(wait, 5))
        return
    for upd in updates:
        state["upd_offset"] = upd["update_id"] + 1
        if upd.get("callback_query"):
            handle_callback(bot, state, upd["callback_query"])
    if updates:
        save_state(state)


_forward_broken = []


def pick_works(res):
    """Ссылки на работы из ответа ИИ (поле works, а если его нет, то из текста отклика)."""
    uname = public_username(CFG.get("portfolio_channel") or "")
    if not uname:
        return []
    src = " ".join(res.get("works") or []) if isinstance(res.get("works"), list) else str(res.get("works") or "")
    src += " " + (res.get("reply") or "")
    ids = []
    for m in re.finditer(r"t\.me/" + re.escape(uname) + r"/(\d+)", src, re.I):
        if int(m.group(1)) not in ids:
            ids.append(int(m.group(1)))
    known = {w["url"].rsplit("/", 1)[1] for w in (load_portfolio.cache or [])}
    if known:
        ids = [i for i in ids if str(i) in known]
    return ids[:3]


def strip_work_links(reply):
    uname = public_username(CFG.get("portfolio_channel") or "")
    if not uname:
        return reply
    reply = re.sub(r"[ \t]*https?://t\.me/" + re.escape(uname) + r"/\d+[ \t]*\n?", "", reply, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", reply).strip()


def forward_works(bot, state, ids, thread_id, sent):
    """Пересылает работы из ТГК портфолио одной пачкой сразу под карточку."""
    uname = public_username(CFG.get("portfolio_channel") or "")
    if not ids or not uname or _forward_broken or not CFG.get("forward_works", True):
        return 0
    try:
        out = bot.call("forwardMessages", chat_id=state["chat_id"], message_thread_id=thread_id,
                       from_chat_id="@" + uname, message_ids=sorted(ids), disable_notification=True)
        sent.extend(m["message_id"] for m in out)
        return len(out)
    except RuntimeError as err:
        _forward_broken.append(1)
        log.warning("Не могу пересылать работы из @%s (%s). Добавь бота админом в этот канал", uname, err)
        return 0


# ---------------- основной цикл ----------------

def reset(bot, state):
    """Удаляет старые карточки бота из группы и забывает прочитанное, чтобы прогнать заново."""
    topics = set(state.get("topics", {}).values())
    probe = bot.call("sendMessage", chat_id=state["chat_id"], text="Обновляю карточки...")
    first = min(topics) + 1 if topics else 1
    ids = [i for i in range(first, probe["message_id"] + 1) if i not in topics]
    ids = sorted(set(ids) | set(state.get("sent_ids", [])) - topics)
    removed = 0
    for k in range(0, len(ids), 100):
        try:
            bot.call("deleteMessages", chat_id=state["chat_id"], message_ids=ids[k:k + 100])
            removed += len(ids[k:k + 100])
        except RuntimeError as err:
            log.warning("Часть сообщений не удалилась: %s", err)
    for key in ("last_ids", "seen", "sigs", "pending", "sent_ids", "cards"):
        state.pop(key, None)
    save_state(state)
    log.info("Старые карточки удалены, начинаю заново")


async def run_once(client, bot, state):
    if not state.get("chat_id"):
        raise SystemExit("Группа ещё не создана. Запусти: python main.py --setup")
    handle_updates(bot, state)
    items = state.get("pending", []) + await collect(client, state)
    state["pending"] = []
    save_state(state)
    portfolio = load_portfolio(state) if items else []
    size = int(CFG["ai"].get("batch_size", 8))
    sent = dropped = 0
    for start in range(0, len(items), size):
        batch = items[start:start + size]
        if start:
            await asyncio.sleep(4)
        results = classify(batch, portfolio)
        if results is None:
            log.error("ИИ недоступен, %d сообщений отложено до следующего запуска", len(items) - start)
            state["pending"] = items[start:]
            break
        by_i = {r.get("i"): r for r in results if isinstance(r, dict)}
        for i, item in enumerate(batch):
            res = by_i.get(i)
            if res is None:
                continue
            try:
                if deliver(bot, state, item, res):
                    sent += 1
                else:
                    dropped += 1
            except Exception as err:
                log.error("Не отправилось: %s", err)
        save_state(state)
    save_state(state)
    log.info("Готово: отправлено %d, отсеяно %d", sent, dropped)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--reset", action="store_true", help="удалить старые карточки и прогнать заново")
    args = ap.parse_args()

    state = load_state()
    bot = Bot(env("BOT_TOKEN"))
    client = make_client()
    if client is not None:
        await client.connect()
        if not await client.is_user_authorized():
            raise SystemExit("Сессия Telegram недействительна. Запусти login.command заново")
    try:
        if args.setup or state.get("chat_id"):
            await setup(client, bot, state)
        if args.setup:
            return
        if args.reset:
            reset(bot, state)
        if args.once:
            await run_once(client, bot, state)
            return
        log.info("Бот работает. Проверка каналов каждые %s мин, кнопки отвечают сразу. Остановить: Ctrl+C",
                 CFG.get("loop_minutes", 5))
        while True:
            try:
                await run_once(client, bot, state)
            except Exception as err:
                log.exception("Ошибка цикла: %s", err)
            deadline = time.time() + int(CFG.get("loop_minutes", 5)) * 60
            while time.time() < deadline:
                handle_updates(bot, state, wait=int(min(25, max(1, deadline - time.time()))))
    finally:
        if client is not None:
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
