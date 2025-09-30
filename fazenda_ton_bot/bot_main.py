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


# ===== MATERIAIS / CONVERSÕES =====
MATERIAIS_DIVISOR = 1000.0        # cada 1000 materiais viram 1 "unidade base"
MATERIAIS_PCT_PAG = 0.40          # 40% vai para Cash de Pagamentos
MATERIAIS_PCT_CASH = 0.60         # 60% vai para Cash Disponível
MATERIAIS_MIN_VENDA = 2000.0      # quantidade mínima para vender


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
    # (preco em cash, rendimento em MATERIAIS/dia)
    animais = [
        ('Galinha', 100,    800,    '🐔'),
        ('Porco',   500,   4000,    '🐖'),
        ('Vaca',   1500,  15000,    '🐄'),
        ('Boi',    2500,  25000,    '🐂'),
        ('Ovelha', 5000,  60000,    '🐑'),
        ('Coelho',10000, 120000,    '🐇'),
        ('Cabra', 20000, 280000,    '🐐'),
        ('Cavalo',50000, 900000,    '🐎'),
    ]
    for nome, preco, rendimento, emoji in animais:
        cur.execute(
            """
            INSERT INTO animais (nome, preco, rendimento, emoji)
            VALUES (?,?,?,?)
            ON CONFLICT(nome) DO UPDATE SET
                preco=excluded.preco,
                rendimento=excluded.rendimento,
                emoji=excluded.emoji
            """,
            (nome, preco, rendimento, emoji)
        )
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
        if not _column_exists(conn, "usuarios", "saldo_materiais"):
            conn.execute("ALTER TABLE usuarios ADD COLUMN saldo_materiais REAL DEFAULT 0.0")

        # Garantir que ultima_coleta não fique nula/vazia
        conn.execute(
            """
            UPDATE inventario
               SET ultima_coleta = COALESCE(ultima_coleta, ?)
             WHERE ultima_coleta IS NULL OR TRIM(ultima_coleta) = ''
            """,
            (datetime.now().isoformat(),)
        )

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
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS cb_tokens (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            payload TEXT,
            expires_at INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
# use .strip() para evitar espaços/linhas acidentais no Render
CRYPTOPAY_TOKEN = (os.getenv("CRYPTOPAY_TOKEN") or "").strip()
CRYPTOPAY_API = "https://pay.crypt.bot/api"

CASH_POR_REAL = int(os.getenv("CASH_POR_REAL", "100"))
REF_PCT = float(os.getenv("REF_PCT", "4"))

# ========= BOT / APP ==========
from aiohttp import ClientTimeout  # (usado em outros pontos)

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

@app.get("/")
async def root():
    return {"ok": True, "service": "fazendinha_bot"}

@app.get("/healthz")
async def healthz():
    return {"ok": True}

# ========= CRYPTO PAY HELPERS =========
def cryptopay_call(method: str, payload: dict):
    try:
        r = requests.post(
            f"{CRYPTOPAY_API}/{method}",
            json=payload,
            headers={"Crypto-Pay-API-Token": CRYPTOPAY_TOKEN},
            timeout=30
        )
        ct = r.headers.get("content-type","")
        data = r.json() if "application/json" in ct else {"ok": False, "description": r.text}
    except Exception as e:
        raise RuntimeError(f"CryptoPay network error on {method}: {e}")

    if not data.get("ok"):
        raise RuntimeError(f"CryptoPay error on {method}: {data}")
    return data["result"]

def get_app_balances():
    """Obtém os saldos do App (TON/USDT etc.). Útil pra depurar payout."""
    return cryptopay_call("getBalance", {})

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

def _balance_code(b: dict) -> str:
    """
    Extrai o código do ativo de um item do getBalance, mesmo que a API use
    um nome de campo diferente. Normaliza para maiúsculas.
    """
    return (
        (b.get("currency")
         or b.get("asset")
         or b.get("currency_code")
         or b.get("code")
         or b.get("ticker")
         or b.get("symbol")
         or "")
        .upper()
    )

def cb_new(user_id: int, action: str, payload: str = "", ttl: int = 600) -> str:
    """
    Cria um token de callback com expiração (TTL em segundos).
    Retorna o token (8-hex).
    """
    token = uuid.uuid4().hex[:8]
    exp = int(time.time()) + ttl
    with db_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO cb_tokens (id, user_id, action, payload, expires_at, used, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (token, user_id, action, payload, exp, _iso_now())
        )
    return token

def cb_check_and_use(token: str, user_id: int, action: str) -> tuple[bool, str | None, str]:
    """
    Valida e marca como 'usado' um token. 
    Retorna (ok, payload, msg_erro).
    """
    now = int(time.time())
    with db_conn() as c:
        row = c.execute(
            "SELECT user_id, action, payload, expires_at, used FROM cb_tokens WHERE id=?",
            (token,)
        ).fetchone()
        if not row:
            return False, None, "Essa interação expirou. Por favor, tente novamente."
        if int(row["user_id"]) != int(user_id) or row["action"] != action:
            return False, None, "Essa interação não é mais válida. Por favor, tente novamente."
        if int(row["used"]) == 1:
            return False, None, "Essa interação já foi usada. Por favor, tente novamente."
        if int(row["expires_at"]) < now:
            return False, None, "Essa interação expirou. Por favor, tente novamente."
        c.execute("UPDATE cb_tokens SET used=1 WHERE id=?", (token,))
        payload = row["payload"] if row["payload"] is not None else ""
        return True, payload, ""



# ===== PRODUTIVIDADE (crescimento com o tempo) =====
def _now():
    return datetime.now()

def _iso_now():
    return _now().isoformat()

def _produzido_desde(rendimento_dia: float, quantidade: int, ultima_coleta_iso: str | None) -> float:
    """
    rendimento_dia: cash/dia de UM animal
    quantidade: número de animais
    ultima_coleta_iso: texto ISO salvo na tabela
    retorna o quanto foi produzido (em cash) desde a ultima_coleta
    """
    try:
        base = datetime.fromisoformat(ultima_coleta_iso) if ultima_coleta_iso else None
    except Exception:
        base = None
    if not base:
        return 0.0
    segundos = (_now() - base).total_seconds()
    if segundos <= 0:
        return 0.0
    por_seg = (rendimento_dia * quantidade) / 86400.0
    return max(0.0, por_seg * segundos)

def get_producao_usuario(uid: int):
    """
    Retorna (itens, total) onde itens é uma lista de dicts:
    {animal, emoji, qtd, produzido}
    """
    with db_conn() as c:
        rows = c.execute("""
            SELECT i.animal, i.quantidade, i.ultima_coleta, a.rendimento, a.emoji
            FROM inventario i
            JOIN animais a ON a.nome = i.animal
            WHERE i.telegram_id = ?
            ORDER BY a.preco ASC
        """, (uid,)).fetchall()

    itens, total = [], 0.0
    for r in rows:
        prod = _produzido_desde(r["rendimento"], int(r["quantidade"]), r["ultima_coleta"])
        itens.append({
            "animal": r["animal"],
            "emoji":  r["emoji"],
            "qtd":    int(r["quantidade"]),
            "produzido": prod,
        })
        total += prod
    return itens, total



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


# === NOVO: ler saldo de Materiais do usuário ===
def get_user_materiais(user_id: int) -> float:
    with db_conn() as c:
        r = c.execute(
            "SELECT COALESCE(saldo_materiais,0) AS m FROM usuarios WHERE telegram_id=?",
            (user_id,)
        ).fetchone()
        return float(r["m"]) if r else 0.0

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
    """
    Envia TON para um endereço on-chain usando createPayout.
    Usa a helper síncrona cryptopay_call com tratamento de erro padronizado.
    """
    payload = {
        "asset": "TON",
        "amount": f"{amount_ton:.9f}",
        "address": ton_address,
        "spend_id": idempotency_key
    }
    try:
        res = cryptopay_call("createPayout", payload)
        return res
    except Exception as e:
        raise CryptoPayError(str(e))

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

def criar_check_ton(amount_ton: float) -> dict:
    """
    Fallback de saque: cria um Check do CryptoBot com TON.
    Retorna o dicionário do result (contém 'check_url').
    """
    payload = {"asset": "TON", "amount": f"{amount_ton:.9f}"}
    return cryptopay_call("createCheck", payload)


# ===== Assinatura do webhook (oficial) =====
def verify_cryptopay_signature(body: bytes, signature: str, token: str) -> bool:
    # HMAC-SHA256(body, sha256(token)) — conforme docs do Crypto Pay
    secret = hashlib.sha256((token or "").encode()).digest()
    computed = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest((signature or "").lower(), computed.lower())

# ========= WEBHOOK CRYPTO PAY =========
@app.post("/webhook/cryptopay")
async def cryptopay_webhook(request: Request):
    # 1) pegar o header correto (com fallback em minúsculas)
    signature = (
        request.headers.get("Crypto-Pay-API-Signature")
        or request.headers.get("crypto-pay-api-signature")
        or ""
    )

    body = await request.body()

    # 2) verifica assinatura (usa SHA256(token) como chave do HMAC)
    if not verify_cryptopay_signature(body, signature, CRYPTOPAY_TOKEN):
        logging.warning("[cryptopay] assinatura inválida")
        return {"ok": False}

    data = await request.json()

    if data.get("update_type") != "invoice_paid":
        return {"ok": True}

    payload = data.get("payload") or {}
    inv = payload.get("invoice") or payload

    invoice_id = str(inv.get("invoice_id") or inv.get("id") or "").strip()
    if not invoice_id:
        logging.warning("[cryptopay] webhook sem invoice_id: %r", data)
        return {"ok": True}

    user_id_str = str(inv.get("payload") or inv.get("custom_payload") or "").strip()
    try:
        user_id = int(user_id_str)
    except Exception:
        logging.warning("[cryptopay] webhook sem user_id(payload): %r", data)
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
            [types.KeyboardButton(text="🔄 Trocas"), types.KeyboardButton(text="🏦 Sacar")],
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

    # agora buscamos também o cash de pagamentos
    cur.execute("SELECT COALESCE(saldo_cash,0), COALESCE(saldo_cash_pagamentos,0), COALESCE(saldo_ton,0) FROM usuarios WHERE telegram_id=?", (user_id,))
    row = cur.fetchone()
    saldo_cash, saldo_pag, saldo_ton = row if row else (0, 0, 0)

    # rendimento/dia (em materiais)
    cur.execute("""
        SELECT SUM(quantidade * rendimento)
        FROM inventario JOIN animais ON inventario.animal = animais.nome
        WHERE inventario.telegram_id=?
    """, (user_id,))
    rendimento_dia = cur.fetchone()[0] or 0

    texto = (
        "🌾 *Bem-vindo à Fazenda TON!*\n\n"
        f"💸 Cash Disponível: *{saldo_cash:.0f}*\n"
        f"🧾 Cash de Pagamentos: *{saldo_pag:.0f}*\n"
        f"💎 TON: *{saldo_ton:.4f}*\n\n"
        f"📈 Rendimento/dia: *{int(rendimento_dia):,}* materiais\n\n"
        "Escolha uma opção:"
    ).replace(",", ".")  # milhar com ponto
    await msg.answer(texto, reply_markup=menu(), parse_mode="Markdown")



@dp.message(F.text == "💰 Meu Saldo")
async def saldo(msg: types.Message):
    user_id = msg.from_user.id
    r = cur.execute("""
        SELECT 
            COALESCE(saldo_cash,0)               AS cash_disp,
            COALESCE(saldo_cash_pagamentos,0)    AS cash_pag,
            COALESCE(saldo_ton,0)                AS saldo_ton,
            COALESCE(saldo_materiais,0)          AS mats
        FROM usuarios WHERE telegram_id=?
    """, (user_id,)).fetchone() or (0,0,0,0)

    cash_disp   = r["cash_disp"] if isinstance(r, sqlite3.Row) else r[0]
    cash_pag    = r["cash_pag"]  if isinstance(r, sqlite3.Row) else r[1]
    saldo_ton   = r["saldo_ton"] if isinstance(r, sqlite3.Row) else r[2]
    materiais   = r["mats"]      if isinstance(r, sqlite3.Row) else r[3]

    texto = (
        "📊 *Seus saldos*\n\n"
        f"• 💸 Cash Disponível: *{cash_disp:.0f}*\n"
        f"• 🧾 Cash de Pagamentos: *{cash_pag:.0f}*\n"
        f"• 🧱 Materiais: *{materiais:.0f}*\n"
        f"• 💎 TON: *{saldo_ton:.6f}*\n\n"
        "Obs.: 🔄 Converta seus 🧱 Materiais em *🔄 Trocas* no menu!\n"
        "Apenas 🧾 Cash de Pagamentos pode ser trocado por TON!"
    )
    await msg.answer(texto, parse_mode="Markdown")




@dp.message(F.text == "🛒 Comprar")
async def comprar(msg: types.Message):
    await msg.answer("Escolha um animal para comprar:", reply_markup=kb_voltar())
    cur.execute("SELECT nome, preco, rendimento, emoji FROM animais ORDER BY preco ASC")
    for nome, preco, rendimento, emoji in cur.fetchall():
        card = (
            f"{emoji} *{nome}*\n"
            f"📈 Rende: *{int(rendimento):,}* Materiais/dia\n"
            f"💵 Preço: *{preco}* cash"
        ).replace(",", ".")  # troca separador milhar para ponto
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

    with db_conn() as c:
        c.execute("""
            UPDATE inventario
               SET ultima_coleta = COALESCE(ultima_coleta, ?)
             WHERE telegram_id = ? AND (ultima_coleta IS NULL OR TRIM(ultima_coleta) = '')
        """, (_iso_now(), user_id))

    itens, total = get_producao_usuario(user_id)

    if not itens:
        return await msg.answer("Você ainda não possui animais. Compre um na loja!")

    linhas = [
        "Acompanhe a produção de *materiais* dos seus animais e colete para converter em *Saldo Cash de Pagamento* 🧾.\n"
        "Não deixe de colher regularmente!\n"
    ]
    for it in itens:
        linhas.append(
            f"{it['emoji']} {it['animal']} (*{it['qtd']}*):  *{it['produzido']:.0f}* 🧱"
        )
    linhas.append(f"\n📈 *Total Produzido:* *{total:.0f}* 🧱")

    # botão com token (ex.: expira em 10 minutos = 30s)
     tok = cb_new(user_id, action="collect", payload="all", ttl=30)
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="📥 Coletar rendimento", callback_data=f"collect:{tok}")
    ]])

    await msg.answer("\n".join(linhas).replace(",", "."), parse_mode="Markdown", reply_markup=kb)



@dp.callback_query(F.data.startswith("collect:"))
async def coletar_rendimento_cb(call: types.CallbackQuery):
    user_id = call.from_user.id

    # valida token
    try:
        _, token = call.data.split(":", 1)
    except Exception:
        return await call.answer("Essa interação expirou. Por favor, tente novamente.", show_alert=True)

    ok, payload, err = cb_check_and_use(token, user_id, action="collect")
    if not ok:
        return await call.answer(err, show_alert=True)


    agora = _iso_now()
    with db_conn() as c:
        # credita nos Materiais
        c.execute("""
            UPDATE usuarios
               SET saldo_materiais = COALESCE(saldo_materiais, 0) + ?
             WHERE telegram_id = ?
        """, (total, user_id))
        # “zera” produção reiniciando o relógio
        c.execute("UPDATE inventario SET ultima_coleta = ? WHERE telegram_id = ?", (agora, user_id))
        r = c.execute("SELECT COALESCE(saldo_materiais,0) AS s FROM usuarios WHERE telegram_id=?",
                      (user_id,)).fetchone()
        novo_saldo = r["s"] if r else 0.0

    await call.message.answer(
        "📥 *Coleta concluída!*\n\n"
        f"• Você coletou: *+{total:.0f}* 🧱\n"
        f"• Materiais agora: *{novo_saldo:.0f}* 🧱\n\n"
        "_Use **🔄 Trocas** para converter Materiais → Cash._",
        parse_mode="Markdown"
    )

    await call.answer()



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

@dp.message(F.text == "🔄 Trocas")
async def trocas_menu(msg: types.Message):
    user_id = msg.from_user.id
    total_mats = int(get_user_materiais(user_id))  # sem separador de milhar

    texto = (
        "Você pode vender sua produção de Materiais e receber 🧾 *Cash de Pagamento*,\n"
        "que podem ser trocados por *TON na segunda opção*.\n"
        "A venda é convertida em dois tipos de saldo (Cash disponível e Cash de pagamento) na seguinte proporção: \n\n"
        "*60%* para o Cash disponível 💸\n"
        "*40%* para o Cash de pagamento 🧾.\n"
        f"Total de Materiais: *{total_mats}* 🧱\n\n"
        f"Taxa de venda: *{int(MATERIAIS_DIVISOR)}* 🧱 = *1*\n"
        f"Quantidade mínima: *{int(MATERIAIS_MIN_VENDA)}* 🧱"
    )

     tok = cb_new(user_id, action="materials", payload="convert_all", ttl=30)
    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🔄 Vender Materiais", callback_data=f"materials:{tok}")],
        [types.InlineKeyboardButton(text="🔄 Trocar cash por TON", callback_data="ton:swap_menu")],
    ])
    await msg.answer(texto, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("materials:"))
async def converter_materiais_cb(call: types.CallbackQuery):
    user_id = call.from_user.id

    # valida token
    try:
        _, token = call.data.split(":", 1)
    except Exception:
        return await call.answer("Essa interação expirou. Por favor, tente novamente.", show_alert=True)

    ok, payload, err = cb_check_and_use(token, user_id, action="materials")
    if not ok:
        return await call.answer(err, show_alert=True)


    # unidades inteiras de (1000) que podem ser convertidas
    unidades = int(mats // MATERIAIS_DIVISOR)        # p.ex.: 9299000 // 1000 => 9299
    usado = int(unidades * MATERIAIS_DIVISOR)        # p.ex.: 9299 * 1000 => 9299000
    sobra = mats - usado                              # resto < 1000 (pode ser float)

    # divisão interna (sem exibir a conta)
    to_pag  = int(unidades * MATERIAIS_PCT_PAG)      # ex.: 40%
    to_cash = int(unidades * MATERIAIS_PCT_CASH)     # ex.: 60%

    # atualiza saldos
    with db_conn() as c:
        c.execute("""
            UPDATE usuarios
               SET saldo_materiais = ?,
                   saldo_cash_pagamentos = COALESCE(saldo_cash_pagamentos,0) + ?,
                   saldo_cash = COALESCE(saldo_cash,0) + ?
             WHERE telegram_id = ?
        """, (sobra, to_pag, to_cash, user_id))

    # mensagem enxuta, sem vírgulas/pontos como separador de milhar
    texto = (
        "✅ Venda de materiais bem sucedida!\n\n"
        f"• Materiais usados: {usado}\n"
        f"   ├─ ✅ Cash de Pagamentos: +{to_pag}\n"
        f"   └─ ✅ Cash Disponível: +{to_cash}\n"
        f"• Materiais restantes: {int(sobra)}"
    )

    await call.message.answer(texto)
    await call.answer()



@dp.callback_query(F.data == "ton:swap_menu")
async def abrir_swap_ton_cb(call: types.CallbackQuery):
    user_id = call.from_user.id
    row = cur.execute(
        "SELECT COALESCE(saldo_cash_pagamentos,0) FROM usuarios WHERE telegram_id=?",
        (user_id,)
    ).fetchone()
    saldo_pag = row[0] if row else 0

    preco_brl = get_ton_price_brl()
    cash_por_ton = max(1, int(round(preco_brl * CASH_POR_REAL)))

    texto = (
        "💱 *Troca cash de pagamentos → TON*\n"
        f"Preço atual: `1 TON ≈ R$ {preco_brl:.2f}`\n"
        f"Equivalência: `1 TON ≈ {cash_por_ton} cash`\n\n"
        f"Seu cash de pagamentos disponível: `{saldo_pag:.0f}`\n\n"
        "Escolha um valor (mín. `20` cash) ou digite: `trocar 250`"
    )

    # 🔒 gera token com validade (TTL) para cada botão
    tok20  = cb_new(user_id, action="swap", payload="20",  ttl=30)
    tok50  = cb_new(user_id, action="swap", payload="50",  ttl=30)
    tok100 = cb_new(user_id, action="swap", payload="100", ttl=30)
    tok500 = cb_new(user_id, action="swap", payload="500", ttl=30)
    tokall = cb_new(user_id, action="swap", payload="all", ttl=30)

    kb = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="20 cash", callback_data=f"swap:20:{tok20}"),
            types.InlineKeyboardButton(text="50",      callback_data=f"swap:50:{tok50}"),
            types.InlineKeyboardButton(text="100",     callback_data=f"swap:100:{tok100}"),
        ],
        [
            types.InlineKeyboardButton(text="500",     callback_data=f"swap:500:{tok500}"),
            types.InlineKeyboardButton(text="Tudo",    callback_data=f"swap:all:{tokall}"),
        ]
    ])
    await call.message.answer(texto, parse_mode="Markdown", reply_markup=kb)
    await call.answer()




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

    # Esperamos "swap:<amount>:<token>"
    try:
        _, amount_s, token = call.data.split(":", 2)
    except Exception:
        return await call.answer("Essa interação expirou. Por favor, tente novamente.", show_alert=True)

    # 🔒 valida o token (expirado/uso único/pertence ao usuário/ação correta)
    ok, payload, err = cb_check_and_use(token, user_id, action="swap")
    if not ok:
        return await call.answer(err, show_alert=True)

    # Segurança extra: confere se o amount do botão bate com o payload gravado
    if (payload or "") != amount_s:
        return await call.answer("Essa interação não é mais válida. Por favor, tente novamente.", show_alert=True)

    # ----------- (se passou, segue seu fluxo atual) -----------
    row = cur.execute(
        "SELECT COALESCE(saldo_cash_pagamentos,0), COALESCE(saldo_ton,0) FROM usuarios WHERE telegram_id=?",
        (user_id,)
    ).fetchone()
    saldo_pag, saldo_ton = row if row else (0, 0)

    preco_brl = get_ton_price_brl()
    cash_por_ton = max(1, int(round(preco_brl * CASH_POR_REAL)))

    amount = saldo_pag if amount_s == "all" else int(amount_s)

    if amount < 20:
        return await call.answer("Mínimo 20 cash.", show_alert=True)
    if amount > saldo_pag:
        return await call.answer("Saldo de pagamentos insuficiente.", show_alert=True)

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

@dp.message(StateFilter(WalletStates.waiting_wallet), F.text == "⬅️ Voltar")
async def cancelar_wallet(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("Voltei ao menu.", reply_markup=menu())

@dp.message(StateFilter(WalletStates.waiting_wallet), F.text == "Pagamento")
async def atalho_pagamento(msg: types.Message, state: FSMContext):
    await state.clear()
    return await iniciar_pagamento(msg, state)

@dp.message(StateFilter(WalletStates.waiting_wallet))
@dp.message(StateFilter(WalletStates.changing_wallet))
async def salvar_wallet(msg: types.Message, state: FSMContext):
    txt = (msg.text or "").strip()

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

    _, _, saldo_ton = get_balances(msg.from_user.id)
    await msg.answer(
        "Quanto você deseja sacar **em TON**?\n\n"
        f"• Saldo TON disponível: {saldo_ton:.6f} TON\n\n"
        "Digite apenas o número (ex.: `0.2`).",
        parse_mode="Markdown"
    )
    await state.set_state(WithdrawStates.waiting_amount_ton)

@dp.message(StateFilter(WithdrawStates.waiting_amount_ton))
async def processar_saque(msg: types.Message, state: FSMContext):
    try:
        amount_ton = float((msg.text or "").replace(",", "."))
        if amount_ton <= 0:
            return await msg.answer("Valor inválido. Envie um número maior que zero.")
    except:
        return await msg.answer("Não entendi. Envie apenas o **número** (ex.: 1.25).")

    if amount_ton < 0.1:
        return await msg.answer("Valor mínimo para saque: 0.1 TON.")

    user_id = msg.from_user.id
    wallet = get_wallet(user_id)
    if not wallet:
        await state.clear()
        return await msg.answer("Wallet não encontrada. Cadastre sua **Wallet TON** e tente novamente.")

    # 1) Checar saldo do App ANTES de reservar o saldo do usuário
    try:
        balances = get_app_balances()
        ton_avail, ton_locked = 0.0, 0.0
        for b in balances:
            code = _balance_code(b)  # <-- usa o helper novo
            if code == "TON":
                ton_avail = float(b.get("available") or 0)
                ton_locked = float(b.get("locked") or 0)
                break

        if ton_avail + 1e-9 < amount_ton:
            await state.clear()
            return await msg.answer(
                f"⚠️ O cofre do app não tem TON suficiente.\n"
                f"Disponível: {ton_avail:.6f} TON | Bloqueado: {ton_locked:.6f} TON\n"
                "Tente um valor menor ou aguarde reabastecimento."
            )
    except Exception as e:
        logging.warning(f"[payout] get_app_balances falhou: {e}")

    
    # 2) Checar e reservar saldo TON do usuário
    with db_conn() as c:
        row = c.execute("SELECT saldo_ton FROM usuarios WHERE telegram_id=?", (user_id,)).fetchone()
        saldo_ton = row["saldo_ton"] if row else 0.0
        if saldo_ton + 1e-9 < amount_ton:
            await state.clear()
            return await msg.answer(
                f"Saldo TON insuficiente para sacar {amount_ton:.6f} TON. "
                f"Seu saldo é {saldo_ton:.6f} TON."
            )
        c.execute("UPDATE usuarios SET saldo_ton = saldo_ton - ? WHERE telegram_id=?", (amount_ton, user_id))

    idemp = new_idempotency_key(user_id)
    wid = create_withdraw(user_id, requested_ton=amount_ton, wallet=wallet, idemp=idemp)
    set_withdraw_status(wid, "processing")
    await msg.answer("⏳ Processando seu saque…")

    try:
        # 3) Payout direto (se habilitado)
        await cryptopay_transfer_ton_to_address(amount_ton, wallet, idemp)
        set_withdraw_status(wid, "done")
        await msg.answer(
            f"✅ Saque enviado!\nValor: {amount_ton:.6f} TON\nCarteira: `{wallet}`",
            parse_mode="Markdown"
        )

    except CryptoPayError as e:
        err = str(e)
        # 4) se não houver payouts habilitados → fallback: criar Check
        if "METHOD_NOT_FOUND" in err or "createPayout" in err or "METHOD_DISABLED" in err:
            try:
                chk = criar_check_ton(amount_ton)
                set_withdraw_status(wid, "done")

                # 1) tente o campo oficial que já vem pronto para o usuário
                link = (
                    chk.get("bot_check_url")
                    or chk.get("check_url")
                    or chk.get("link")
                    or (f"https://t.me/CryptoBot?start=check_{chk.get('hash')}" if chk.get("hash") else "")
                )

                # 2) se mesmo assim não tiver link, avise claramente o admin
                if not link:
                    # estorna o saldo do usuário, pois não conseguimos entregar o link
                    with db_conn() as c:
                        c.execute(
                            "UPDATE usuarios SET saldo_ton = saldo_ton + ? WHERE telegram_id=?",
                            (amount_ton, user_id)
                        )
                    set_withdraw_status(wid, "failed")
                    return await msg.answer(
                        "❌ Check criado, mas não recebi o link de resgate da API.\n"
                        "Avise o suporte/admin para verificar o método createCheck e os campos retornados.",
                    )

                # 3) envie um botão com o link (evita problemas de parse_mode)
                kb = types.InlineKeyboardMarkup(
                    inline_keyboard=[[types.InlineKeyboardButton(text="🔗 Resgatar no @CryptoBot", url=link)]]
                )
                await msg.answer(
                    "✅ Saque criado como *Check do CryptoBot*.\n\n"
                    "Toque no botão abaixo para resgatar o TON na sua carteira do @CryptoBot.\n"
                    "Depois você pode sacar on-chain para qualquer endereço.",
                    parse_mode="Markdown",
                    reply_markup=kb
                )

            except Exception as ee:
                # falhou até o fallback → estorna
                with db_conn() as c:
                    c.execute(
                        "UPDATE usuarios SET saldo_ton = saldo_ton + ? WHERE telegram_id=?",
                        (amount_ton, user_id)
                    )
                set_withdraw_status(wid, "failed")
                await msg.answer(
                    "❌ Não foi possível completar o saque agora (fallback para Check falhou).\n"
                    f"Detalhe: `{str(ee)[:200]}`\n"
                    "O valor foi estornado para seu saldo TON. Tente novamente mais tarde.",
                    parse_mode="Markdown"
                )

        else:
            # outro erro qualquer → estorna
            with db_conn() as c:
                c.execute(
                    "UPDATE usuarios SET saldo_ton = saldo_ton + ? WHERE telegram_id=?",
                    (amount_ton, user_id)
                )
            set_withdraw_status(wid, "failed")
            await msg.answer(
                "❌ Não foi possível completar o saque agora.\n"
                f"Detalhe: `{err[:200]}`\n"
                "O valor foi estornado para seu saldo TON. Tente novamente mais tarde.",
                parse_mode="Markdown"
            )
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
        "• 🛒 Comprar animais com Cash Disponível\n"
        "• 💰 Depositar via Crypto Pay (USDT/TON cobrados em BRL)\n"
        "• 🔄 Trocas (Materiais → Cash e Cash de Pagamentos → TON)\n"
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

@dp.message(Command("appsaldo"))
async def app_saldo(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    try:
        bals = get_app_balances()
        linhas = []
        for b in bals:
            code = _balance_code(b) or "?"
            avail = b.get("available") or 0
            locked = b.get("locked") or 0
            linhas.append(f"{code}: disponível {avail} | bloqueado {locked}")
        texto = "💼 Saldos do App:\n" + "\n".join(linhas)
        await msg.answer(texto)
 
    except Exception as e:
        await msg.answer(f"Erro ao obter saldos do app: {e}")

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
