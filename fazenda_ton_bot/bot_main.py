import os
import asyncio
import sqlite3
from datetime import datetime
import hashlib
import hmac
import requests

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

from fastapi import FastAPI, Request
import uvicorn

# ========= CONFIG ==========
# Telegram
TOKEN = os.getenv('TOKEN')  # token do bot do Telegram
OWNER_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0"))
def is_admin(uid: int) -> bool:
    return OWNER_ID and uid == OWNER_ID

# App público (URL do Render depois do 1º deploy)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # ex: https://seu-servico.onrender.com

BOT_USERNAME = os.getenv("BOT_USERNAME", "SEU_BOT_USERNAME")  # ex.: "bot_kjp7"

# Banco de Dados
DB_PATH = os.getenv("DB_PATH", "fazenda.db")

# Crypto Pay (https://t.me/CryptoBot -> Crypto Pay -> Create App)
CRYPTOPAY_TOKEN = os.getenv("CRYPTOPAY_TOKEN")
CRYPTOPAY_API = "https://pay.crypt.bot/api"

# Conversão simples: 1 real = X cash
CASH_POR_REAL = float(os.getenv("CASH_POR_REAL", "100"))

# Indicações – % que o indicador recebe sobre cada depósito do indicado
REF_PCT = float(os.getenv("REF_PCT", "4"))   # ex.: 4 = 4%


# ========= DB ==========
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


    # Relação de quem indicou quem
    cur.execute('''CREATE TABLE IF NOT EXISTS indicacoes (
        quem INTEGER PRIMARY KEY,   -- id do novo usuário
        por  INTEGER,               -- id do indicador
        criado_em TEXT
    )''')

    
    # Evitar crédito duplicado de depósitos
    cur.execute('''CREATE TABLE IF NOT EXISTS pagamentos (
        invoice_id TEXT PRIMARY KEY,
        user_id INTEGER,
        valor_reais REAL,
        cash INTEGER,
        criado_em TEXT
    )''')
    cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_pagamentos_invoice
                   ON pagamentos(invoice_id)''')

    # Fila de saques
    cur.execute('''CREATE TABLE IF NOT EXISTS saques (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        valor_ton REAL,
        carteira TEXT,
        status TEXT DEFAULT 'pendente',   -- pendente | pago | cancelado
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

init_db()
cadastrar_animais()

# ========= BOT ==========
bot = Bot(token=TOKEN)
dp = Dispatcher()

# ========= FASTAPI ==========
app = FastAPI()

@app.get("/")
async def root():
    return {"ok": True, "service": "fazendinha_bot"}

@app.get("/healthz")
async def healthz():
    return {"ok": True}

# ========= CRYPTO PAY HELPERS ==========
def cryptopay_call(method: str, payload: dict):
    """Chama a API do Crypto Pay. Lança erro se vier ok=False."""
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
    """
    Cria uma invoice FIAT (BRL) com aceitação de USDT e TON.
    Retorna a URL para o usuário pagar (bot_invoice_url).
    """
    payload = {
        "currency_type": "fiat",
        "fiat": "BRL",
        "amount": f"{valor_reais:.2f}",
        "accepted_assets": "USDT,TON",
        "payload": str(user_id),            # será retornado no webhook
        "description": "Depósito Fazendinha"
    }
    inv = cryptopay_call("createInvoice", payload)  # objeto Invoice
    return inv.get("bot_invoice_url") or inv.get("pay_url")

def verify_cryptopay_signature(body_bytes: bytes, signature_hex: str, token: str) -> bool:
    """
    Verifica assinatura do webhook do Crypto Pay.
    Assinatura = HMAC-SHA256(body) com secret = SHA256(token).
    Header: crypto-pay-api-signature
    """
    try:
        secret = hashlib.sha256(token.encode()).digest()
        calc = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
        return (signature_hex or "").lower() == calc.lower()
    except Exception:
        return False

# ========= WEBHOOK CRYPTO PAY ==========
@app.post("/webhook/cryptopay")
async def cryptopay_webhook(request: Request):
    data = await request.json()

    # Aceita só quando efetivamente pago
    if data.get("update_type") != "invoice_paid":
        return {"ok": True}

    # --- Tenta achar o objeto da invoice em formatos diferentes ---
    payload = data.get("payload") or {}
    inv = payload.get("invoice") or payload  # alguns webhooks vem com payload.invoice, outros direto no payload

    # invoice_id (string)
    invoice_id = str(inv.get("invoice_id") or inv.get("id") or "").strip()
    if not invoice_id:
        print("[cryptopay] webhook sem invoice_id:", data)
        return {"ok": True}

    # user_id (vem no campo payload/custom_payload que você enviou em createInvoice)
    user_id_str = str(inv.get("payload") or inv.get("custom_payload") or "").strip()
    try:
        user_id = int(user_id_str)
    except Exception:
        print("[cryptopay] webhook sem user_id(payload):", data)
        return {"ok": True}

    # Valor em reais: usa o primeiro que existir
    raw_reais = (
        inv.get("price_amount") or   # fiat
        inv.get("fiat_amount")  or
        inv.get("amount")       or   # alguns formatos
        inv.get("paid_amount")  or
        0
    )
    try:
        reais = float(raw_reais)
    except Exception:
        reais = 0.0

    # Converte para cash
    cash = int(round(reais * CASH_POR_REAL))

    # 1) Idempotência (se já processou esta invoice, sai)
    try:
        cur.execute(
            "INSERT INTO pagamentos (invoice_id, user_id, valor_reais, cash, criado_em) "
            "VALUES (?, ?, ?, ?, ?)",
            (invoice_id, user_id, reais, cash, datetime.now().isoformat())
        )
        con.commit()
    except sqlite3.IntegrityError:
        return {"ok": True}

    # 2) Garante que o usuário existe mesmo que nunca tenha usado /start
    cur.execute(
        "INSERT OR IGNORE INTO usuarios (telegram_id, criado_em) VALUES (?, ?)",
        (user_id, datetime.now().isoformat())
    )

    # 3) Credita o depósito
    if cash > 0:
        cur.execute(
            "UPDATE usuarios SET saldo_cash = COALESCE(saldo_cash,0) + ? WHERE telegram_id=?",
            (cash, user_id)
        )

    # 4) Bônus de indicação (4% por padrão via REF_PCT)
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

    # 5) Aviso ao pagador (só se teve cash>0)
    if cash > 0:
        try:
            await bot.send_message(
                user_id,
                f"✅ Pagamento confirmado!\nR$ {reais:.2f} → {cash} cash creditados."
            )
        except Exception:
            pass

    return {"ok": True}




# ========= UI / MENUS ==========
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


# ========= HANDLERS ==========
@dp.message(Command('start'))
async def start(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute(
        "INSERT OR IGNORE INTO usuarios (telegram_id, criado_em) VALUES (?, ?)",
        (user_id, datetime.now().isoformat()))
    con.commit()

    # Se veio com payload /start <indicador>, registra a indicação (uma vez)
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
    saldo_cash, saldo_ton = cur.fetchone() or (0, 0)

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


@dp.message(lambda msg: msg.text == "💰 Meu Saldo")
async def saldo(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute("SELECT COALESCE(saldo_cash,0), COALESCE(saldo_ton,0) FROM usuarios WHERE telegram_id=?", (user_id,))
    row = cur.fetchone()
    saldo_cash, saldo_ton = row if row else (0, 0)
    await msg.answer(
        f"💸 Seu saldo em cash: `{saldo_cash:.0f}`\n"
        f"💎 Seu saldo em TON: `{saldo_ton:.4f}`\n"
        f"Conversão (depósito): 1 real = {CASH_POR_REAL:.0f} cash",
        parse_mode="Markdown"
    )

@dp.message(lambda msg: msg.text == "🛒 Comprar")
async def comprar(msg: types.Message):
    # mostra só o botão Voltar no teclado de baixo
    await msg.answer("Escolha um animal para comprar:", reply_markup=kb_voltar())

    cur.execute("SELECT nome, preco, rendimento, emoji FROM animais ORDER BY preco ASC")
    for nome, preco, rendimento, emoji in cur.fetchall():
        card = (
            f"{emoji} *{nome}*\n"
            f"💵 Preço: `{preco}` cash\n"
            f"📈 Rende: `{rendimento}` cash/dia"
        )
        kb_inline = types.InlineKeyboardMarkup(inline_keyboard=[[
            types.InlineKeyboardButton(
                text=f"Comprar {emoji}",
                callback_data=f"buy:{nome}"
            )
        ]])
        await msg.answer(card, reply_markup=kb_inline, parse_mode="Markdown")

@dp.callback_query(lambda c: c.data and c.data.startswith("buy:"))
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

    # Debita e adiciona ao inventário
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
    await call.answer()  # confirma o clique



@dp.message(lambda msg: msg.text == "⬅️ Voltar")
async def voltar(msg: types.Message):
    await start(msg)

@dp.message(lambda msg: msg.text == "🐾 Meus Animais")
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
@dp.message(lambda msg: msg.text == "➕ Depositar")
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

@dp.message(lambda msg: msg.text in ["R$ 10","R$ 25","R$ 50","R$ 100"])
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

@dp.message(lambda msg: msg.text == "Outro valor (R$)")
async def outro_valor(msg: types.Message):
    await msg.answer("Envie o valor desejado em reais. Ex.: 37,90")

@dp.message(lambda msg: _parse_reais(msg.text) is not None and "R$" not in msg.text and "TON" not in msg.text)
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

# ===== trocas/saques etc. (mantive seu fluxo) =====
@dp.message(lambda msg: msg.text == "🔄 Trocar cash por TON")
async def trocar_cash(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute("SELECT COALESCE(saldo_cash,0), COALESCE(saldo_ton,0) FROM usuarios WHERE telegram_id=?", (user_id,))
    saldo_cash, saldo_ton = cur.fetchone()
    if saldo_cash < 1000:
        await msg.answer("Você precisa de pelo menos 1000 cash para trocar por TON.\nCada 1000 cash = 1 TON")
        return
    ton_adicionado = int(saldo_cash // 1000)
    novo_cash = saldo_cash % 1000
    novo_ton = saldo_ton + ton_adicionado
    cur.execute("UPDATE usuarios SET saldo_cash=?, saldo_ton=? WHERE telegram_id=?", (novo_cash, novo_ton, user_id))
    con.commit()
    await msg.answer(
        f"🔄 Troca realizada!\nAgora você tem `{novo_cash:.0f}` cash e `{novo_ton:.4f}` TON.\n(Use o menu principal para sacar)",
        parse_mode="Markdown"
    )

@dp.message(lambda msg: msg.text == "🏦 Sacar")
async def sacar(msg: types.Message):
    await msg.answer(
        "Envie o valor em TON **e** a carteira TON na mesma mensagem.\n"
        "Exemplo:\n`2.0 UQxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`\n\n"
        "_Dica: a carteira TON geralmente começa com `UQ` ou `EQ`._",
        parse_mode="Markdown"
    )


@dp.message(lambda msg: msg.text == "👫 Indique & Ganhe")
async def indicacao(msg: types.Message):
    user_id = msg.from_user.id

    # Conta quantas pessoas esse usuário indicou
    row = cur.execute("SELECT COUNT(*) FROM indicacoes WHERE por=?", (user_id,)).fetchone()
    total_refs = row[0] if row else 0

    # Link de convite (deep link)
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"

    texto = (
        "🎁 <b>Indique & Ganhe</b>\n\n"
        f"Convide amigos e receba <b>{REF_PCT:.0f}%</b> de cada depósito que eles fizerem.\n\n"
        f"👥 <b>Indicações:</b> {total_refs}\n"
        f"🔗 <b>Seu link:</b> <code>{link}</code>"
    )

    # Usa HTML para evitar problemas com underscores no link
    await msg.answer(texto, parse_mode="HTML")


@dp.message(lambda msg: msg.text == "❓ Ajuda/Suporte")
async def ajuda(msg: types.Message):
    await msg.answer(
        "Dúvidas? Fale com o suporte: @seu_suporte\n\n"
        "• 🛒 Comprar animais com cash\n"
        "• 💰 Depositar via Crypto Pay (USDT/TON cobrados em BRL)\n"
        "• 🔄 Trocar cash por TON\n"
        "• 🏦 Sacar TON para sua carteira\n\n"
        "Qualquer dúvida, fale conosco!"
    )

# Heurística: mensagem com número + espaço + endereço TON (evita conflitar com depósito custom)
@dp.message(lambda m: m.text and len(m.text.strip().split()) >= 2 and m.text.strip().split()[0].replace('.', '', 1).isdigit())
async def processa_saque(msg: types.Message):
    partes = msg.text.strip().split()
    try:
        valor = float(partes[0])
        carteira = partes[1]
    except Exception:
        await msg.answer("Formato inválido. Envie: `VALOR CARTEIRA_TON`", parse_mode="Markdown")
        return

    # validações simples
    if valor <= 0:
        await msg.answer("Valor inválido.")
        return
    if not (carteira.startswith("UQ") or carteira.startswith("EQ")) or len(carteira) < 36:
        await msg.answer("Carteira TON aparentemente inválida. Verifique e envie novamente.")
        return

    user_id = msg.from_user.id
    cur.execute("SELECT saldo_ton FROM usuarios WHERE telegram_id=?", (user_id,))
    r = cur.fetchone()
    saldo = r[0] if r else 0.0

    if saldo < valor:
        await msg.answer(f"Saldo insuficiente. Seu saldo TON é `{saldo:.4f}`.", parse_mode="Markdown")
        return

    # Reserva: debita já para não gastar duas vezes
    cur.execute("UPDATE usuarios SET saldo_ton = saldo_ton - ? WHERE telegram_id=?", (valor, user_id))
    cur.execute(
        "INSERT INTO saques (telegram_id, valor_ton, carteira, status, criado_em) VALUES (?, ?, ?, 'pendente', ?)",
        (user_id, valor, carteira, datetime.now().isoformat())
    )
    saque_id = cur.lastrowid
    con.commit()

    await msg.answer(
        f"✅ Pedido de saque **#{saque_id}** criado.\n"
        f"Valor: `{valor:.4f}` TON\nCarteira: `{carteira}`\n\n"
        f"Acompanhe com `/meussaques`.",
        parse_mode="Markdown"
    )

    # avisa o admin
    if OWNER_ID:
        try:
            await bot.send_message(
                OWNER_ID,
                f"🔔 Novo saque pendente #{saque_id}\nUser {user_id}\n{valor:.4f} TON → {carteira}"
            )
        except Exception:
            pass


@dp.message(Command("meussaques"))
async def meus_saques(msg: types.Message):
    rows = cur.execute(
        "SELECT id, valor_ton, carteira, status, criado_em, IFNULL(pago_em, '') "
        "FROM saques WHERE telegram_id=? ORDER BY id DESC LIMIT 10",
        (msg.from_user.id,)
    ).fetchall()
    if not rows:
        await msg.answer("Você ainda não tem pedidos de saque.")
        return

    linhas = []
    for (sid, val, cart, status, criado, pago) in rows:
        quando = (criado or "").split("T")[0]
        extra = f" • pago em {(pago or '').split('T')[0]}" if (pago or "").strip() else ""
        linhas.append(f"#{sid} • {val:.4f} TON • {status}{extra}")
    await msg.answer("🧾 *Seus últimos saques:*\n" + "\n".join(linhas), parse_mode="Markdown")


@dp.message(Command("adm_saques"))
async def adm_saques(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    rows = cur.execute(
        "SELECT id, telegram_id, valor_ton, carteira, criado_em "
        "FROM saques WHERE status='pendente' ORDER BY id"
    ).fetchall()
    if not rows:
        await msg.answer("Sem saques pendentes.")
        return
    linhas = [f"#{sid} • user {uid} • {val:.4f} TON • {carteira}" for (sid, uid, val, carteira, _) in rows]
    await msg.answer(
        "⏳ *Pendentes:*\n" + "\n".join(linhas) + "\n\nMarcar pago: `/pagar ID`",
        parse_mode="Markdown"
    )

@dp.message(Command("pagar"))
async def pagar_cmd(msg: types.Message):
    if not is_admin(msg.from_user.id):
        return
    p = msg.text.strip().split()
    if len(p) != 2 or not p[1].isdigit():
        await msg.answer("Use: `/pagar ID`", parse_mode="Markdown")
        return
    sid = int(p[1])
    row = cur.execute(
        "SELECT telegram_id, valor_ton, carteira, status FROM saques WHERE id=?",
        (sid,)
    ).fetchone()
    if not row:
        await msg.answer("ID não encontrado.")
        return
    uid, val, cart, status = row
    if status != "pendente":
        await msg.answer("Esse saque não está pendente.")
        return

    cur.execute("UPDATE saques SET status='pago', pago_em=? WHERE id=?", (datetime.now().isoformat(), sid))
    con.commit()
    await msg.answer(f"✅ Saque #{sid} marcado como *PAGO*.", parse_mode="Markdown")

    try:
        await bot.send_message(uid, f"✅ Seu saque #{sid} foi enviado.\nValor: {val:.4f} TON\nCarteira: {cart}")
    except Exception:
        pass

# ========= INICIAR BOT ==========
def start_bot():
    asyncio.create_task(dp.start_polling(bot))

@app.on_event("startup")
async def on_startup():
    await bot.delete_webhook(drop_pending_updates=True)
    start_bot()

# ========== FASTAPI MAIN ==========
if __name__ == '__main__':
    # Execução local (dev). No Render, use o Gunicorn no Start Command.
    uvicorn.run("fazenda_ton_bot.bot_main:app", host="0.0.0.0", port=8000, reload=True)
