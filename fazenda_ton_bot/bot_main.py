import asyncio
from datetime import datetime
import hashlib
import hmac
import requests
import httpx
import re, uuid, time, os, sqlite3
import logging
logging.basicConfig(level=logging.INFO)


from aiogram import Bot, Dispatcher, F, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from fastapi import FastAPI, Request
import uvicorn


# ===== Preço do TON em BRL – robusto, com cache, retries e múltiplas fontes =====

PRICE_CACHE_SECONDS = int(os.getenv("PRICE_CACHE_SECONDS", "60"))  # TTL do cache (s)
FALLBACK_TON_BRL = float(os.getenv("FALLBACK_TON_BRL", "17.0"))

_TON_CACHE = {"price": FALLBACK_TON_BRL, "ts": 0.0}

COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price?symbol=TONUSDT"
OKX_TICKER     = "https://www.okx.com/api/v5/market/ticker?instId=TON-USDT"
FX_USD_BRL_1 = "https://open.er-api.com/v6/latest/USD"
FX_USD_BRL_2 = "https://api.exchangerate.host/latest?base=USD&symbols=BRL"

def _is_sane_brl(p: float) -> bool:
    return 0.1 <= p <= 1000.0

def _try_with_retries(fn, attempts=(0.0, 0.5, 1.0)):
    for delay in attempts:
        try:
            v = float(fn())
            if v > 0:
                return v
        except Exception:
            pass
        if delay:
            time.sleep(delay)
    return 0.0

def _cg_simple_brl():
    r = requests.get(
        COINGECKO_SIMPLE,
        params={"ids": "the-open-network", "vs_currencies": "brl", "precision": "full"},
        timeout=10,
    )
    r.raise_for_status()
    return float(r.json()["the-open-network"]["brl"])

def _cg_markets_brl():
    r = requests.get(
        COINGECKO_MARKETS,
        params={"vs_currency": "brl", "ids": "the-open-network"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return float(data[0]["current_price"]) if isinstance(data, list) and data else 0.0

def _binance_ton_usdt():
    r = requests.get(BINANCE_TICKER, timeout=10)
    r.raise_for_status()
    return float(r.json()["price"])

def _okx_ton_usdt():
    r = requests.get(OKX_TICKER, timeout=10)
    r.raise_for_status()
    data = r.json()
    return float(data["data"][0]["last"]) if "data" in data and data["data"] else 0.0

def _usd_brl_rate():
    def _fx1():
        r = requests.get(FX_USD_BRL_1, timeout=10)
        r.raise_for_status()
        return float(r.json()["rates"]["BRL"])
    def _fx2():
        r = requests.get(FX_USD_BRL_2, timeout=10)
        r.raise_for_status()
        return float(r.json()["rates"]["BRL"])
    rate = _try_with_retries(_fx1)
    if rate <= 0:
        rate = _try_with_retries(_fx2)
    return rate

def _ton_brl_from_usdt_paths():
    usdt_price = _try_with_retries(_binance_ton_usdt)
    if usdt_price <= 0:
        usdt_price = _try_with_retries(_okx_ton_usdt)
    if usdt_price <= 0:
        return 0.0
    usd_brl = _try_with_retries(_usd_brl_rate)
    if usd_brl <= 0:
        return 0.0
    return usdt_price * usd_brl

def get_ton_price_brl() -> float:
    now = time.time()
    if (now - _TON_CACHE["ts"] < PRICE_CACHE_SECONDS) and _TON_CACHE["price"] > 0:
        return _TON_CACHE["price"]

    price = _try_with_retries(_cg_simple_brl)
    if not _is_sane_brl(price):
        price = 0.0

    if price <= 0.0:
        price = _try_with_retries(_cg_markets_brl)
        if not _is_sane_brl(price):
            price = 0.0

    if price <= 0.0:
        price = _try_with_retries(_ton_brl_from_usdt_paths)
        if not _is_sane_brl(price):
            price = 0.0

    if price > 0.0:
        _TON_CACHE["price"] = price
        _TON_CACHE["ts"] = now

    return _TON_CACHE["price"]

PRICE_REFRESH_SECONDS = int(os.getenv("PRICE_REFRESH_SECONDS", "45"))

async def _refresh_price_loop():
    while True:
        try:
            _ = get_ton_price_brl()
        except Exception:
            pass
        await asyncio.sleep(PRICE_REFRESH_SECONDS)


# ========= CONFIG ==========
TOKEN = os.getenv('TOKEN')
OWNER_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0"))
def is_admin(uid: int) -> bool:
    return OWNER_ID and uid == OWNER_ID

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "SEU_BOT_USERNAME")

# ========= DB ==========
DB_PATH = os.getenv("DB_PATH", "/data/db.sqlite3")
print("DB_PATH em uso:", DB_PATH)

db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)


def _column_exists(conn, table, column):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r[1] == column for r in cur.fetchall())

# conexão global usada por partes do código
con = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = con.cursor()

def init_db():
    cur.execute('''CREATE TABLE IF NOT EXISTS usuarios (
        telegram_id INTEGER PRIMARY KEY,
        saldo_cash REAL DEFAULT 0,
        saldo_ton REAL DEFAULT 0,
        carteira_ton TEXT,
        criado_em TEXT
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS animais (
        nome TEXT PRIMARY KEY,
        preco INTEGER,
        rendimento REAL,
        emoji TEXT
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS inventario (
        telegram_id INTEGER,
        animal TEXT,
        quantidade INTEGER DEFAULT 0,
        ultima_coleta TEXT,
        PRIMARY KEY (telegram_id, animal)
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS indicacoes (
        quem INTEGER PRIMARY KEY,
        por  INTEGER,
        criado_em TEXT
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS pagamentos (
        invoice_id TEXT PRIMARY KEY,
        user_id INTEGER,
        valor_reais REAL,
        cash INTEGER,
        criado_em TEXT
    )''')
    cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_pagamentos_invoice
                   ON pagamentos(invoice_id)''')

    cur.execute('''CREATE TABLE IF NOT EXISTS saques (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        valor_ton REAL,
        carteira TEXT,
        status TEXT DEFAULT 'pendente',
        criado_em TEXT,
        pago_em TEXT
    )''')

    con.commit()

def cadastrar_animais():
    animais = [
        ('Galinha', 100, 2, '🐔'), ('Porco', 500, 10, '🐖'),
        ('Vaca', 1500, 30, '🐄'), ('Boi', 2500, 50, '🐂'),
        ('Ovelha', 5000, 100, '🐑'), ('Coelho', 10000, 200, '🐇'),
        ('Cabra', 15000, 300, '🐐'), ('Cavalo', 20000, 400, '🐎')
    ]
    for nome, preco, rendimento, emoji in animais:
        cur.execute(
            "INSERT OR IGNORE INTO animais (nome, preco, rendimento, emoji) VALUES (?, ?, ?, ?)",
            (nome, preco, rendimento, emoji))
    con.commit()

def ensure_schema():
    conn = sqlite3.connect(DB_PATH)
    try:
        if not _column_exists(conn, "usuarios", "carteira_ton"):
            conn.execute("ALTER TABLE usuarios ADD COLUMN carteira_ton TEXT")
        if not _column_exists(conn, "usuarios", "saldo_cash"):
            conn.execute("ALTER TABLE usuarios ADD COLUMN saldo_cash REAL DEFAULT 0.0")
        if not _column_exists(conn, "usuarios", "saldo_cash_pagamentos"):
            conn.execute("ALTER TABLE usuarios ADD COLUMN saldo_cash_pagamentos REAL DEFAULT 0.0")
        if not _column_exists(conn, "usuarios", "saldo_ton"):
            conn.execute("ALTER TABLE usuarios ADD COLUMN saldo_ton REAL DEFAULT 0.0")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL,
          requested_ton REAL NOT NULL,
          wallet TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('pending','processing','done','failed')) DEFAULT 'pending',
          idempotency_key TEXT UNIQUE NOT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.commit()
    finally:
        conn.close()

# cria tudo primeiro, depois prossegue
init_db()
cadastrar_animais()
ensure_schema()


# ========= CRYPTO PAY ==========
CRYPTOPAY_TOKEN = os.getenv("CRYPTOPAY_TOKEN")
CRYPTOPAY_API = "https://pay.crypt.bot/api"

CASH_POR_REAL = int(os.getenv("CASH_POR_REAL", "100"))
REF_PCT = float(os.getenv("REF_PCT", "4"))


# ========= BOT / APP ==========
from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import ClientTimeout  # <-- adicione esta importação

_session = AiohttpSession(timeout=ClientTimeout(total=10))
bot = Bot(token=TOKEN, session=_session)

dp = Dispatcher()

app = FastAPI()

@app.get("/")
async def root():
    return {"ok": True, "service": "fazendinha_bot"}

@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ========= CRYPTO PAY HELPERS ==========
def cryptopay_call(method: str, payload: dict):
    r = requests.post(
        f"{CRYPTOPAY_API}/{method}",
        json=payload,
        headers={"Crypto-Pay-API-Token": CRYPTOPAY_TOKEN},
        timeout=30
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"CryptoPay error: {data}")
    return data["result"]

def criar_invoice_cryptopay(user_id: int, valor_reais: float) -> str:
    payload = {
        "currency_type": "fiat",
        "fiat": "BRL",
        "amount": f"{valor_reais:.2f}",
        "accepted_assets": "USDT,TON",
        "payload": str(user_id),
        "description": "Depósito Fazendinha"
    }
    inv = cryptopay_call("createInvoice", payload)
    return inv.get("bot_invoice_url") or inv.get("pay_url")

def ensure_user(user_id: int):
    with db_conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO usuarios (telegram_id, criado_em) VALUES (?, ?)",
            (user_id, datetime.now().isoformat())
        )


# === TECLADOS / BOTÕES ===
def sacar_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Wallet TON"), KeyboardButton(text="Pagamento")],
            [KeyboardButton(text="⬅️ Voltar")]
        ],
        resize_keyboard=True
    )

def alterar_wallet_inline():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Alterar Wallet", callback_data="alterar_wallet")]
        ]
    )


# === STATES (FSM) ===
class WalletStates(StatesGroup):
    waiting_wallet = State()
    changing_wallet = State()

class WithdrawStates(StatesGroup):
    waiting_amount_ton = State()


# === UTILS ===
WALLET_RE = re.compile(r'^[UE]Q[A-Za-z0-9_-]{45,}$')

def is_valid_ton_wallet(addr: str) -> bool:
    addr = addr.strip()
    return bool(WALLET_RE.match(addr))

def normalize_wallet(addr: str) -> str:
    return re.sub(r'\s+', '', addr.strip())

def new_idempotency_key(user_id: int) -> str:
    return f"wd-{user_id}-{int(time.time())}-{uuid.uuid4().hex[:8]}"

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def set_wallet(user_id: int, wallet: str):
    ensure_user(user_id)
    with db_conn() as c:
        c.execute("UPDATE usuarios SET carteira_ton=? WHERE telegram_id=?", (wallet, user_id))

def get_wallet(user_id: int):
    with db_conn() as c:
        r = c.execute("SELECT carteira_ton FROM usuarios WHERE telegram_id=?", (user_id,)).fetchone()
        return r["carteira_ton"] if r and r["carteira_ton"] else None

def get_balances(user_id: int):
    with db_conn() as c:
        r = c.execute("SELECT saldo_cash, saldo_cash_pagamentos, saldo_ton FROM usuarios WHERE telegram_id=?", (user_id,)).fetchone()
        if not r: return 0.0, 0.0, 0.0
        return r["saldo_cash"], r["saldo_cash_pagamentos"], r["saldo_ton"]

def debit_cash_payments_and_credit_ton(user_id: int, amount_ton: float, ton_brl_price: float, cash_por_real: int):
    cash_needed = amount_ton * ton_brl_price * cash_por_real
    with db_conn() as c:
        row = c.execute("SELECT saldo_cash_pagamentos, saldo_ton FROM usuarios WHERE telegram_id=?", (user_id,)).fetchone()
        if not row:
            raise ValueError("Usuário não encontrado.")
        if row["saldo_cash_pagamentos"] + 1e-9 < cash_needed:
            raise ValueError("Saldo de pagamentos insuficiente.")
        c.execute(
            "UPDATE usuarios SET saldo_cash_pagamentos=saldo_cash_pagamentos-?, saldo_ton=saldo_ton+? WHERE telegram_id=?",
            (cash_needed, amount_ton, user_id)
        )

def create_withdraw(user_id: int, requested_ton: float, wallet: str, idemp: str):
    with db_conn() as c:
        c.execute("""INSERT INTO withdrawals (user_id, requested_ton, wallet, status, idempotency_key)
                     VALUES (?,?,?,?,?)""", (user_id, requested_ton, wallet, 'pending', idemp))
        return c.execute("SELECT last_insert_rowid() id").fetchone()["id"]

def set_withdraw_status(wid: int, status: str):
    with db_conn() as c:
        c.execute("UPDATE withdrawals SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, wid))


class CryptoPayError(Exception):
    pass

async def cryptopay_transfer_ton_to_address(amount_ton: float, ton_address: str, idempotency_key: str):
    headers = {
        "Crypto-Pay-API-Token": CRYPTOPAY_TOKEN
    }
    payload = {
        "asset": "TON",
        "amount": str(amount_ton),
        "address": ton_address,
        "spend_id": idempotency_key  # idempotência correta
    }
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post("https://pay.crypt.bot/api/createPayout", headers=headers, json=payload)
        data = r.json() if r.headers.get("content-type","").startswith("application/json") else {"ok": False, "description": r.text}
        if r.status_code != 200 or not data.get("ok"):
            raise CryptoPayError(f"createPayout falhou: {data}")
        return data["result"]


async def cryptopay_transfer_ton_to_user(amount_ton: float, crypto_user_id: int, idempotency_key: str):
    headers = {
        "Crypto-Pay-API-Token": CRYPTOPAY_TOKEN,
        "Idempotency-Key": idempotency_key
    }
    payload = {"asset": "TON", "amount": str(amount_ton), "user_id": crypto_user_id}
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post("https://pay.crypt.bot/api/transfer", headers=headers, json=payload)
        data = r.json() if r.headers.get("content-type","").startswith("application/json") else {"ok": False, "description": r.text}
        if r.status_code != 200 or not data.get("ok"):
            raise CryptoPayError(f"transfer falhou: {data}")
        return data["result"]


def verify_cryptopay_signature(body_bytes: bytes, signature_hex: str, token: str) -> bool:
    try:
        secret = hashlib.sha256(token.encode()).digest()
        calc = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
        return (signature_hex or "").lower() == calc.lower()
    except Exception:
        return False


# ========= WEBHOOK CRYPTO PAY =========
@app.post("/webhook/cryptopay")
async def cryptopay_webhook(request: Request):
    data = await request.json()

    if data.get("update_type") != "invoice_paid":
        return {"ok": True}

    payload = data.get("payload") or {}
    inv = payload.get("invoice") or payload

    invoice_id = str(inv.get("invoice_id") or inv.get("id") or "").strip()
    if not invoice_id:
        print("[cryptopay] webhook sem invoice_id:", data)
        return {"ok": True}

    user_id_str = str(inv.get("payload") or inv.get("custom_payload") or "").strip()
    try:
        user_id = int(user_id_str)
    except Exception:
        print("[cryptopay] webhook sem user_id(payload):", data)
        return {"ok": True}

    raw_reais = (
        inv.get("price_amount") or
        inv.get("fiat_amount")  or
        inv.get("amount")       or
        inv.get("paid_amount")  or
        0
    )
    try:
        reais = float(raw_reais)
    except Exception:
        reais = 0.0

    cash = int(round(reais * CASH_POR_REAL))

    try:
        cur.execute(
            "INSERT INTO pagamentos (invoice_id, user_id, valor_reais, cash, criado_em) VALUES (?,?,?,?,?)",
            (invoice_id, user_id, reais, cash, datetime.now().isoformat())
        )
        con.commit()
    except sqlite3.IntegrityError:
        return {"ok": True}

    cur.execute(
        "INSERT OR IGNORE INTO usuarios (telegram_id, criado_em) VALUES (?, ?)",
        (user_id, datetime.now().isoformat())
    )

    if cash > 0:
        cur.execute(
            "UPDATE usuarios SET saldo_cash = COALESCE(saldo_cash,0) + ? WHERE telegram_id=?",
            (cash, user_id)
        )

    cur.execute("SELECT por FROM indicacoes WHERE quem=?", (user_id,))
    row = cur.fetchone()
    if row:
        ref_id = int(row[0])
        bonus = int(round(cash * REF_PCT / 100.0))
        if bonus > 0:
            cur.execute(
                "INSERT OR IGNORE INTO usuarios (telegram_id, criado_em) VALUES (?, ?)",
                (ref_id, datetime.now().isoformat())
            )
            cur.execute(
                "UPDATE usuarios SET saldo_cash = COALESCE(saldo_cash,0) + ? WHERE telegram_id=?",
                (bonus, ref_id)
            )
            try:
                await bot.send_message(
                    ref_id,
                    f"🎁 Bônus de indicação: +{bonus} cash (amigo depositou R$ {reais:.2f})."
                )
            except Exception:
                pass

    con.commit()

    if cash > 0:
        try:
            await bot.send_message(
                user_id,
                f"✅ Pagamento confirmado!\nR$ {reais:.2f} → {cash} cash creditados."
            )
        except Exception:
            pass

    return {"ok": True}


# ========= UI / MENUS =========
def kb_voltar():
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="⬅️ Voltar")]],
        resize_keyboard=True
    )

def menu():
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="🐾 Meus Animais"), types.KeyboardButton(text="💰 Meu Saldo")],
            [types.KeyboardButton(text="🛒 Comprar"), types.KeyboardButton(text="➕ Depositar")],
            [types.KeyboardButton(text="🔄 Trocar cash por TON"), types.KeyboardButton(text="🏦 Sacar")],
            [types.KeyboardButton(text="👫 Indique & Ganhe"), types.KeyboardButton(text="❓ Ajuda/Suporte")]
        ],
        resize_keyboard=True
    )


# ========= HANDLERS =========
@dp.message(Command('start'))
async def start(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute(
        "INSERT OR IGNORE INTO usuarios (telegram_id, criado_em) VALUES (?, ?)",
        (user_id, datetime.now().isoformat())
    )
    con.commit()

    parts = (msg.text or "").split()
    ref_id = None
    if len(parts) >= 2 and parts[0] == '/start' and parts[1].isdigit():
        ref_id = int(parts[1])
    if ref_id and ref_id != user_id:
        cur.execute(
            "INSERT OR IGNORE INTO indicacoes (quem, por, criado_em) VALUES (?, ?, ?)",
            (user_id, ref_id, datetime.now().isoformat())
        )
        con.commit()

    cur.execute("SELECT saldo_cash, saldo_ton FROM usuarios WHERE telegram_id=?", (user_id,))
    row = cur.fetchone()
    saldo_cash, saldo_ton = row if row else (0, 0)

    # 🔧 estas duas linhas precisam estar alinhadas com as de cima (sem indent extra)
    cur.execute("SELECT SUM(quantidade) FROM inventario WHERE telegram_id=?", (user_id,))
    total_animais = cur.fetchone()[0] or 0

    cur.execute("""
        SELECT SUM(quantidade * rendimento)
        FROM inventario JOIN animais ON inventario.animal = animais.nome
        WHERE inventario.telegram_id=?
    """, (user_id,))
    rendimento_dia = cur.fetchone()[0] or 0

    texto = (
        "🌾 *Bem-vindo à Fazenda TON!*\n\n"
        f"💸 Cash: `{saldo_cash:.0f}`\n"
        f"💎 TON: `{saldo_ton:.4f}`\n"
        f"🐾 Animais: `{total_animais}`\n"
        f"📈 Rendimento/dia: `{rendimento_dia:.2f}` cash\n\n"
        "Escolha uma opção:"
    )
    await msg.answer(texto, reply_markup=menu(), parse_mode="Markdown")



@dp.message(F.text == "💰 Meu Saldo")
async def saldo(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute("""
        SELECT 
            COALESCE(saldo_cash,0)               AS cash_depositos,
            COALESCE(saldo_cash_pagamentos,0)    AS cash_pag,
            COALESCE(saldo_ton,0)                AS saldo_ton
        FROM usuarios WHERE telegram_id=?
    """, (user_id,))
    r = cur.fetchone() or (0,0,0)
    cash_dep, cash_pag, saldo_ton = r

    await msg.answer(
        "📊 *Seus saldos*\n\n"
        f"• 💸 Cash (depósitos / compra de animais): `{cash_dep:.0f}`\n"
        f"• 🧾 Cash pagamentos (rendimentos): `{cash_pag:.0f}`\n"
        f"• 💎 TON (conversões do cash pagamentos): `{saldo_ton:.6f}`\n\n"
        f"_Obs.: Apenas o saldo TON pode ser sacado. O botão **🔄 Trocar cash por TON** usa **cash pagamentos**._",
        parse_mode="Markdown"
    )


@dp.message(F.text == "🛒 Comprar")
async def comprar(msg: types.Message):
    await msg.answer("Escolha um animal para comprar:", reply_markup=kb_voltar())
    cur.execute("SELECT nome, preco, rendimento, emoji FROM animais ORDER BY preco ASC")
    for nome, preco, rendimento, emoji in cur.fetchall():
        card = (
            f"{emoji} *{nome}*\n"
            f"💵 Preço: `{preco}` cash\n"
            f"📈 Rende: `{rendimento}` cash/dia"
        )
        kb_inline = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(text=f"Comprar {emoji}", callback_data=f"buy:{nome}")
        ]])
        await msg.answer(card, reply_markup=kb_inline, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("buy:"))
async def comprar_animal_cb(call: types.CallbackQuery):
    nome = call.data.split("buy:", 1)[1]
    user_id = call.from_user.id

    cur.execute("SELECT preco, rendimento, emoji FROM animais WHERE nome=?", (nome,))
    r = cur.fetchone()
    if not r:
        await call.answer("Animal não encontrado.", show_alert=True)
        return
    preco, rendimento, emoji = r

    cur.execute("SELECT saldo_cash FROM usuarios WHERE telegram_id=?", (user_id,))
    row = cur.fetchone()
    saldo = row[0] if row else 0

    if saldo < preco:
        await call.message.answer(f"⚠️ Cash insuficientes para comprar {emoji}!")
        await call.answer("Saldo insuficiente", show_alert=False)
        return

    cur.execute("UPDATE usuarios SET saldo_cash=saldo_cash-? WHERE telegram_id=?", (preco, user_id))
    agora = datetime.now().isoformat()
    cur.execute(
        "INSERT OR IGNORE INTO inventario (telegram_id, animal, quantidade, ultima_coleta) VALUES (?, ?, 0, ?)",
        (user_id, nome, agora)
    )
    cur.execute(
        "UPDATE inventario SET quantidade=quantidade+1, ultima_coleta=? WHERE telegram_id=? AND animal=?",
        (agora, user_id, nome)
    )
    con.commit()

    await call.message.answer(f"✅ Você comprou com sucesso {emoji}!")
    await call.answer()


@dp.message(F.text == "⬅️ Voltar")
async def voltar(msg: types.Message):
    await start(msg)

@dp.message(F.text == "🐾 Meus Animais")
async def meus_animais(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute("SELECT animal, quantidade FROM inventario WHERE telegram_id=?", (user_id,))
    itens = cur.fetchall()
    if not itens:
        await msg.answer("Você ainda não possui animais. Compre um na loja!")
        return
    resposta = "🐾 *Seus Animais:*\n\n"
    total_rendimento = 0
    for animal, qtd in itens:
        cur.execute("SELECT rendimento, emoji FROM animais WHERE nome=?", (animal,))
        rendimento, emoji = cur.fetchone()
        resposta += f"{emoji} {animal}: `{qtd}` | Rendimento: `{rendimento * qtd:.1f} cash/dia`\n"
        total_rendimento += rendimento * qtd
    resposta += f"\n📈 *Total rendimento/dia:* `{total_rendimento:.1f} cash`"
    await msg.answer(resposta, parse_mode="Markdown")


# ===== Depósito via Crypto Pay (BRL) =====
@dp.message(StateFilter(None), F.text == "➕ Depositar")
async def depositar_menu(msg: types.Message):
    kb = types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text="R$ 10"), types.KeyboardButton(text="R$ 25")],
            [types.KeyboardButton(text="R$ 50"), types.KeyboardButton(text="R$ 100")],
            [types.KeyboardButton(text="Outro valor (R$)"), types.KeyboardButton(text="⬅️ Voltar")]
        ],
        resize_keyboard=True
    )
    await msg.answer("Escolha o valor do depósito em **reais**:", reply_markup=kb, parse_mode="Markdown")

def _parse_reais(txt: str):
    t = txt.upper().replace("R$", "").strip().replace(",", ".")
    try:
        v = float(t)
        return v if v > 0 else None
    except:
        return None

@dp.message(StateFilter(None), F.text.in_(["R$ 10","R$ 25","R$ 50","R$ 100"]))
async def gerar_link_padrao(msg: types.Message):
    if not CRYPTOPAY_TOKEN:
        await msg.answer("Configuração de pagamento ausente. Avise o suporte.")
        return
    val = _parse_reais(msg.text)
    try:
        url = criar_invoice_cryptopay(msg.from_user.id, val)
    except Exception:
        await msg.answer("Erro ao criar cobrança. Tente novamente.")
        return
    await msg.answer(
        f"💸 Depósito de R$ {val:.2f}\nPague por aqui:\n{url}\n\n"
        "Assim que o pagamento for confirmado, eu credito seus cash. ⏳"
    )

@dp.message(StateFilter(None), F.text == "Outro valor (R$)")
async def outro_valor(msg: types.Message):
    await msg.answer("Envie o valor desejado em reais. Ex.: 37,90")

@dp.message(StateFilter(None), lambda m: _parse_reais(m.text) is not None and "R$" not in m.text and "TON" not in m.text)
async def gerar_link_custom(msg: types.Message):
    if not CRYPTOPAY_TOKEN:
        await msg.answer("Configuração de pagamento ausente. Avise o suporte.")
        return
    val = _parse_reais(msg.text)
    if not val or val < 1:
        await msg.answer("Valor mínimo: R$ 1,00.")
        return
    try:
        url = criar_invoice_cryptopay(msg.from_user.id, val)
    except Exception:
        await msg.answer("Erro ao criar cobrança. Tente novamente.")
        return
    await msg.answer(
        f"💸 Depósito de R$ {val:.2f}\nPague por aqui:\n{url}\n\n"
        "Crédito automático após confirmação. ⏳"
    )


# ===== Troca cash -> TON =====
@dp.message(F.text == "🔄 Trocar cash por TON")
async def trocar_cash(msg: types.Message):
    user_id = msg.from_user.id
    row = cur.execute(
        "SELECT COALESCE(saldo_cash_pagamentos,0), COALESCE(saldo_ton,0) FROM usuarios WHERE telegram_id=?",
        (user_id,)
    ).fetchone()
    saldo_pag, saldo_ton = row if row else (0, 0)

    preco_brl = get_ton_price_brl()
    cash_por_ton = max(1, int(round(preco_brl * CASH_POR_REAL)))

    texto = (
        "💱 *Troca cash pagamentos → TON*\n"
        f"Preço atual (CoinGecko): `1 TON ≈ R$ {preco_brl:.2f}`\n"
        f"Equivalência: `1 TON ≈ {cash_por_ton} cash`\n\n"
        f"Seu cash pagamentos disponível: `{saldo_pag:.0f}`\n\n"
        "Escolha um valor (mín. `20` cash) ou digite: `trocar 250`"
    )

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="20 cash", callback_data="swap:20"),
            types.InlineKeyboardButton(text="50", callback_data="swap:50"),
            types.InlineKeyboardButton(text="100", callback_data="swap:100"),
        ],
        [
            types.InlineKeyboardButton(text="500", callback_data="swap:500"),
            types.InlineKeyboardButton(text="Tudo", callback_data="swap:all"),
        ]
    ])
    await msg.answer(texto, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("swap:"))
async def swap_cb(call: types.CallbackQuery):
    user_id = call.from_user.id
    row = cur.execute(
        "SELECT COALESCE(saldo_cash_pagamentos,0), COALESCE(saldo_ton,0) FROM usuarios WHERE telegram_id=?",
        (user_id,)
    ).fetchone()
    saldo_pag, saldo_ton = row if row else (0, 0)

    preco_brl = get_ton_price_brl()
    cash_por_ton = max(1, int(round(preco_brl * CASH_POR_REAL)))

    _, amount_s = call.data.split(":", 1)
    amount = saldo_pag if amount_s == "all" else int(amount_s)

    if amount < 20:
        await call.answer("Mínimo 20 cash.", show_alert=True)
        return
    if amount > saldo_pag:
        await call.answer("Saldo de pagamentos insuficiente.", show_alert=True)
        return

    ton_out = amount / cash_por_ton
    cur.execute(
        "UPDATE usuarios SET saldo_cash_pagamentos=saldo_cash_pagamentos-?, saldo_ton=saldo_ton+? WHERE telegram_id=?",
        (amount, ton_out, user_id)
    )

    con.commit()

    await call.message.answer(
        f"✅ Convertidos `{amount}` cash pagamentos → `{ton_out:.5f}` TON\n"
        f"`1 TON = {cash_por_ton} cash` (≈ R$ {preco_brl:.2f}).",
        parse_mode="Markdown"
    )
    await call.answer()

@dp.message(lambda m: m.text and m.text.lower().startswith("trocar "))
async def trocar_texto(msg: types.Message):
    try:
        parts = msg.text.strip().split()
        amount = int(parts[1])
    except Exception:
        await msg.answer("Formato: `trocar 250` (mín. 20 cash)", parse_mode="Markdown")
        return

    user_id = msg.from_user.id
    row = cur.execute(
        "SELECT COALESCE(saldo_cash_pagamentos,0), COALESCE(saldo_ton,0) FROM usuarios WHERE telegram_id=?",
        (user_id,)
    ).fetchone()
    saldo_pag, saldo_ton = row if row else (0, 0)

    if amount < 20:
        await msg.answer("Mínimo 20 cash.")
        return
    if amount > saldo_pag:
        await msg.answer("Saldo de pagamentos insuficiente.")
        return

    preco_brl = get_ton_price_brl()
    cash_por_ton = max(1, int(round(preco_brl * CASH_POR_REAL)))
    ton_out = amount / cash_por_ton

    cur.execute(
        "UPDATE usuarios SET saldo_cash_pagamentos=saldo_cash_pagamentos-?, saldo_ton=saldo_ton+? WHERE telegram_id=?",
        (amount, ton_out, user_id)
    )
    con.commit()

    await msg.answer(
        f"✅ Convertidos `{amount}` cash pagamentos → `{ton_out:.5f}` TON\n"
        f"`1 TON = {cash_por_ton} cash` (≈ R$ {preco_brl:.2f}).",
        parse_mode="Markdown"
    )



# ===== Saque =====
@dp.message(F.text == "🏦 Sacar")
async def sacar_menu(msg: types.Message):
    await msg.answer("Escolha uma opção de saque:", reply_markup=sacar_keyboard())

@dp.message(F.text == "Wallet TON")
async def pedir_wallet(msg: types.Message, state: FSMContext):
    wal = get_wallet(msg.from_user.id)
    if wal:
        await msg.answer(
            f"Carteira atual:\n`{wal}`\n\nSe quiser alterar, toque em **Alterar Wallet**.",
            parse_mode="Markdown"
        )
        await msg.answer(
            "Para alterar sua carteira TON, toque no botão abaixo.",
            reply_markup=alterar_wallet_inline()
        )
    else:
        await msg.answer(
            "Envie agora **seu endereço de carteira TON** para receber os saques (ex.: começa com `UQ` ou `EQ`).",
            parse_mode="Markdown",
            reply_markup=kb_voltar()
        )
        await state.set_state(WalletStates.waiting_wallet)

@dp.callback_query(F.data == "alterar_wallet")
async def alterar_wallet_cb(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Envie o **novo endereço de carteira TON** para saque.", parse_mode="Markdown")
    await cb.message.answer("Você pode voltar quando quiser.", reply_markup=kb_voltar())
    await state.set_state(WalletStates.changing_wallet)
    await cb.answer()

# sair do fluxo de wallet e voltar ao menu
@dp.message(StateFilter(WalletStates.waiting_wallet), F.text == "⬅️ Voltar")
async def cancelar_wallet(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("Voltei ao menu.", reply_markup=menu())

# ir para Pagamento mesmo estando no fluxo de wallet
@dp.message(StateFilter(WalletStates.waiting_wallet), F.text == "Pagamento")
async def atalho_pagamento(msg: types.Message, state: FSMContext):
    await state.clear()
    return await iniciar_pagamento(msg, state)

@dp.message(StateFilter(WalletStates.waiting_wallet))
@dp.message(StateFilter(WalletStates.changing_wallet))
async def salvar_wallet(msg: types.Message, state: FSMContext):
    txt = (msg.text or "").strip()

    # atalhos durante a captura
    if txt in {"Pagamento", "Wallet TON", "⬅️ Voltar"}:
        if txt == "⬅️ Voltar":
            await state.clear()
            return await msg.answer("Voltei ao menu.", reply_markup=menu())
        if txt == "Pagamento":
            await state.clear()
            return await iniciar_pagamento(msg, state)
        return await msg.answer(
            "Envie o endereço de carteira TON (começa com UQ/EQ) ou toque em ⬅️ Voltar.",
            reply_markup=sacar_keyboard()
        )

    addr = normalize_wallet(txt)
    if not is_valid_ton_wallet(addr):
        return await msg.answer(
            "Endereço inválido. Certifique-se que começa com `UQ` ou `EQ` e tente novamente.",
            parse_mode="Markdown"
        )

    set_wallet(msg.from_user.id, addr)
    await state.clear()
    await msg.answer(f"✅ Carteira salva:\n`{addr}`", parse_mode="Markdown", reply_markup=alterar_wallet_inline())
    await msg.answer("Pronto! Use o menu abaixo.", reply_markup=menu())

@dp.message(F.text == "Pagamento")
async def iniciar_pagamento(msg: types.Message, state: FSMContext):
    await state.clear()

    wal = get_wallet(msg.from_user.id)
    if not wal:
        return await msg.answer(
            "Você ainda não definiu sua **Wallet TON**. Toque em *Wallet TON* e cadastre antes de sacar.",
            parse_mode="Markdown",
            reply_markup=sacar_keyboard()
        )

    _, saldo_pag, saldo_ton = get_balances(msg.from_user.id)
    await msg.answer(
        "Quanto você deseja sacar **em TON**?\n\n"
        f"• Saldo de pagamentos (cash dos animais): {saldo_pag:.2f} cash\n"
        f"• Saldo TON: {saldo_ton:.6f} TON\n\n"
        "Obs.: só é permitido sacar o valor **gerado pelos animais**. No saque, converteremos do *cash de pagamentos* para TON.",
        parse_mode="Markdown"
    )
    await state.set_state(WithdrawStates.waiting_amount_ton)

@dp.message(StateFilter(WithdrawStates.waiting_amount_ton))
async def processar_saque(msg: types.Message, state: FSMContext):
    try:
        amount_ton = float(msg.text.replace(",", "."))
        if amount_ton <= 0:
            return await msg.answer("Valor inválido. Envie um número maior que zero.")
    except:
        return await msg.answer("Não entendi. Envie apenas o **número** (ex.: 1.25).")

    wallet = get_wallet(msg.from_user.id)
    if not wallet:
        await state.clear()
        return await msg.answer("Wallet não encontrada. Cadastre sua **Wallet TON** e tente novamente.")

    ton_brl = get_ton_price_brl()

    try:
        debit_cash_payments_and_credit_ton(
            user_id=msg.from_user.id,
            amount_ton=amount_ton,
            ton_brl_price=ton_brl,
            cash_por_real=CASH_POR_REAL
        )
    except Exception as e:
        await state.clear()
        return await msg.answer(f"Saldo insuficiente nos **pagamentos** para converter {amount_ton:.6f} TON. ({e})")

    idemp = new_idempotency_key(msg.from_user.id)
    wid = create_withdraw(msg.from_user.id, requested_ton=amount_ton, wallet=wallet, idemp=idemp)
    set_withdraw_status(wid, "processing")
    await msg.answer("⏳ Processando seu saque…")

    try:
        await cryptopay_transfer_ton_to_address(amount_ton, wallet, idemp)
        set_withdraw_status(wid, "done")
        await msg.answer(f"✅ Saque enviado!\nValor: {amount_ton:.6f} TON\nCarteira: `{wallet}`", parse_mode="Markdown")

    except Exception:
        with db_conn() as c:
            c.execute(
                "UPDATE usuarios SET saldo_cash_pagamentos = saldo_cash_pagamentos + (? * ? * ?), saldo_ton = saldo_ton - ? WHERE telegram_id=?",
                (amount_ton, ton_brl, CASH_POR_REAL, amount_ton, msg.from_user.id)
            )
        set_withdraw_status(wid, "failed")
        await msg.answer("❌ Não foi possível completar o saque agora (payout indisponível). O valor foi estornado para seu saldo de pagamentos. Tente novamente mais tarde.")
    finally:
        await state.clear()


@dp.message(F.text == "👫 Indique & Ganhe")
async def indicacao(msg: types.Message):
    user_id = msg.from_user.id
    row = cur.execute("SELECT COUNT(*) FROM indicacoes WHERE por=?", (user_id,)).fetchone()
    total_refs = row[0] if row else 0
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    texto = (
        "🎁 <b>Indique & Ganhe</b>\n\n"
        f"Convide amigos e receba <b>{REF_PCT:.0f}%</b> de cada depósito que eles fizerem.\n\n"
        f"👥 <b>Indicações:</b> {total_refs}\n"
        f"🔗 <b>Seu link:</b> <code>{link}</code>"
    )
    await msg.answer(texto, parse_mode="HTML")

@dp.message(F.text == "❓ Ajuda/Suporte")
async def ajuda(msg: types.Message):
    await msg.answer(
        "Dúvidas? Fale com o suporte: @seu_suporte\n\n"
        "• 🛒 Comprar animais com cash\n"
        "• 💰 Depositar via Crypto Pay (USDT/TON cobrados em BRL)\n"
        "• 🔄 Trocar cash por TON\n"
        "• 🏦 Sacar TON para sua carteira\n\n"
        "Qualquer dúvida, fale conosco!"
    )

# ===== COMANDOS DE ADMIN =====
@dp.message(Command("addcash"))
async def add_cash(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    try:
        _, uid, valor = msg.text.split()
        uid = int(uid)
        valor = int(valor)
    except:
        return await msg.answer("Uso: /addcash <user_id> <valor>")
    cur.execute("UPDATE usuarios SET saldo_cash=COALESCE(saldo_cash,0)+? WHERE telegram_id=?", (valor, uid))
    con.commit()
    await msg.answer(f"✅ Adicionado {valor} cash ao usuário {uid}")

@dp.message(Command("addpag"))
async def add_pag(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    try:
        _, uid, valor = msg.text.split()
        uid = int(uid)
        valor = int(valor)
    except:
        return await msg.answer("Uso: /addpag <user_id> <valor>")
    cur.execute("UPDATE usuarios SET saldo_cash_pagamentos=COALESCE(saldo_cash_pagamentos,0)+? WHERE telegram_id=?", (valor, uid))
    con.commit()
    await msg.answer(f"✅ Adicionado {valor} cash_pagamentos ao usuário {uid}")

@dp.message(Command("addton"))
async def add_ton(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    try:
        _, uid, valor = msg.text.split()
        uid = int(uid)
        valor = float(valor)
    except:
        return await msg.answer("Uso: /addton <user_id> <valor>")
    cur.execute("UPDATE usuarios SET saldo_ton=COALESCE(saldo_ton,0)+? WHERE telegram_id=?", (valor, uid))
    con.commit()
    await msg.answer(f"✅ Adicionado {valor} TON ao usuário {uid}")


# ========= INICIAR BOT =========
async def _run_polling_forever():
    backoff = 1
    while True:
        try:
            logging.info("[polling] iniciando polling…")
            await dp.start_polling(bot)
        except Exception as e:
            logging.error("[polling] caiu: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


@app.on_event("startup")
async def on_startup():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("[startup] webhook deletado")
    except Exception as e:
        logging.warning("[startup] delete_webhook falhou, mas vou ignorar: %s", e)

    asyncio.create_task(_run_polling_forever())
    asyncio.create_task(_refresh_price_loop())


# ========== FASTAPI MAIN ==========
if __name__ == '__main__':
    uvicorn.run("fazenda_ton_bot.bot_main:app", host="0.0.0.0", port=8000, reload=True)
