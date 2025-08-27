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

# App público (URL do Render depois do 1º deploy)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")  # ex: https://seu-servico.onrender.com

# Banco de Dados
DB_PATH = os.getenv("DB_PATH", "fazenda.db")

# Crypto Pay (https://t.me/CryptoBot -> Crypto Pay -> Create App)
CRYPTOPAY_TOKEN = os.getenv("CRYPTOPAY_TOKEN")
CRYPTOPAY_API = "https://pay.crypt.bot/api"

# Conversão simples: 1 real = X cash
CASH_POR_REAL = float(os.getenv("CASH_POR_REAL", "100"))

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
    raw = await request.body()
    sig = request.headers.get("crypto-pay-api-signature")
    if not CRYPTOPAY_TOKEN or not verify_cryptopay_signature(raw, sig, CRYPTOPAY_TOKEN):
        # assinatura inválida ou token ausente -> ignora
        return {"ok": True}

    data = await request.json()
    if data.get("update_type") == "invoice_paid":
        inv = data.get("payload", {})  # objeto Invoice
        # user_id que colocamos em payload na criação
        try:
            user_id = int(inv.get("payload", "0"))
        except Exception:
            user_id = 0

        # Se a invoice foi criada em BRL (currency_type=fiat), geralmente "amount" é em fiat
        try:
            reais = float(inv.get("amount", "0"))
        except Exception:
            reais = 0.0

        cash = int(reais * CASH_POR_REAL)
        if user_id and cash > 0:
            cur.execute(
                "UPDATE usuarios SET saldo_cash = COALESCE(saldo_cash,0) + ? WHERE telegram_id = ?",
                (cash, user_id)
            )
            con.commit()
            try:
                await bot.send_message(
                    user_id,
                    f"✅ Pagamento confirmado!\nR$ {reais:.2f} → `{cash}` cash creditados.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    return {"ok": True}

# ========= UI / MENUS ==========
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
        (user_id, datetime.now().isoformat())
    )
    con.commit()

    cur.execute("SELECT COALESCE(saldo_cash,0), COALESCE(saldo_ton,0) FROM usuarios WHERE telegram_id=?", (user_id,))
    row = cur.fetchone()
    saldo_cash, saldo_ton = row if row else (0, 0)

    cur.execute("SELECT SUM(quantidade) FROM inventario WHERE telegram_id=?", (user_id,))
    total_animais = cur.fetchone()[0] or 0

    cur.execute("""SELECT SUM(quantidade * rendimento)
                   FROM inventario JOIN animais ON inventario.animal = animais.nome
                   WHERE inventario.telegram_id=?""", (user_id,))
    rendimento_dia = cur.fetchone()[0] or 0

    texto = (
        f"🌾 *Bem-vindo à Fazenda TON!*\n\n"
        f"💸 Cash: `{saldo_cash:.0f}` | 💎 TON: `{saldo_ton:.4f}`\n"
        f"🐾 Animais: `{total_animais}` | 📈 Rendimento/dia: `{rendimento_dia:.2f} cash`\n\n"
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
    cur.execute("SELECT nome, preco, rendimento, emoji FROM animais")
    animais = cur.fetchall()
    texto = "*Escolha o animal para comprar:*\n\n"
    keyboard = []
    for nome, preco, rendimento, emoji in animais:
        texto += f"{emoji} {nome} — `{preco} cash` | Rende `{rendimento} cash/dia`\n"
        keyboard.append([types.KeyboardButton(text=f"{emoji} Comprar {nome}")])
    keyboard.append([types.KeyboardButton(text="⬅️ Voltar")])
    await msg.answer(
        texto,
        reply_markup=types.ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True),
        parse_mode="Markdown"
    )

@dp.message(lambda msg: msg.text and msg.text.startswith(tuple(['🐔','🐖','🐄','🐂','🐑','🐇','🐐','🐎'])) and "Comprar" in msg.text)
async def comprar_animal(msg: types.Message):
    user_id = msg.from_user.id
    nome = msg.text.split("Comprar ", 1)[1]
    cur.execute("SELECT preco FROM animais WHERE nome=?", (nome,))
    r = cur.fetchone()
    if not r:
        await msg.answer("Animal não encontrado.")
        return
    preco = r[0]

    cur.execute("SELECT COALESCE(saldo_cash,0) FROM usuarios WHERE telegram_id=?", (user_id,))
    row = cur.fetchone()
    saldo = row[0] if row else 0  # corrigido: não chamar fetchone() duas vezes

    if saldo < preco:
        await msg.answer("❌ Saldo insuficiente.")
        return

    cur.execute("UPDATE usuarios SET saldo_cash=saldo_cash-? WHERE telegram_id=?", (preco, user_id))
    cur.execute("INSERT OR IGNORE INTO inventario (telegram_id, animal, quantidade, ultima_coleta) VALUES (?, ?, 0, ?)",
                (user_id, nome, datetime.now().isoformat()))
    cur.execute("UPDATE inventario SET quantidade=quantidade+1, ultima_coleta=? WHERE telegram_id=? AND animal=?",
                (datetime.now().isoformat(), user_id, nome))
    con.commit()
    await msg.answer(f"Parabéns! Você comprou um(a) {nome} 🎉", reply_markup=menu())

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
        "Envie o valor em TON e a carteira TON para sacar. Exemplo:\n`2 UQxxxxxxxxxxxx`",
        parse_mode="Markdown"
    )

@dp.message(lambda msg: msg.text == "👫 Indique & Ganhe")
async def indicacao(msg: types.Message):
    user_id = msg.from_user.id
    link = f"https://t.me/seu_bot?start={user_id}"
    await msg.answer(f"Convide amigos com este link e ganhe bônus:\n{link}")

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
@dp.message(lambda msg: isinstance(msg.text, str) and ' ' in msg.text
            and msg.text.split(' ')[0].replace('.', '', 1).isdigit()
            and len(msg.text.split(' ')[1]) >= 30)
async def processa_saque(msg: types.Message):
    try:
        partes = msg.text.strip().split(' ')
        valor = float(partes[0])
        carteira = partes[1]
    except Exception:
        await msg.answer("Formato inválido. Envie: VALOR CARTEIRA_TON")
        return
    user_id = msg.from_user.id
    cur.execute("SELECT COALESCE(saldo_ton,0) FROM usuarios WHERE telegram_id=?", (user_id,))
    saldo = cur.fetchone()[0]
    if saldo < valor:
        await msg.answer("Saldo insuficiente.")
        return
    cur.execute("UPDATE usuarios SET saldo_ton=saldo_ton-? WHERE telegram_id=?", (valor, user_id))
    con.commit()
    await msg.answer(f"Saque de {valor:.4f} TON para a carteira {carteira} está sendo processado.\n\n(Simulação MVP)")

# ========= INICIAR BOT ==========
def start_bot():
    asyncio.create_task(dp.start_polling(bot))

@app.on_event("startup")
async def on_startup():
    start_bot()

# ========== FASTAPI MAIN ==========
if __name__ == '__main__':
    # Execução local (dev). No Render, use o Gunicorn no Start Command.
    uvicorn.run("fazenda_ton_bot.bot_main:app", host="0.0.0.0", port=8000, reload=True)
