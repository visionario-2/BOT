import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import sqlite3
from datetime import datetime
from fastapi import FastAPI, Request
import uvicorn

# ========= CONFIG ==========
TOKEN = os.getenv('TOKEN')
XROCKET_API_TOKEN = os.getenv('XROCKET_API_TOKEN')

con = sqlite3.connect('fazenda.db', check_same_thread=False)
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
    animais = [('Galinha', 100, 2, '🐔'), ('Porco', 500, 10, '🐖'),
               ('Vaca', 1500, 30, '🐄'), ('Boi', 2500, 50, '🐂'),
               ('Ovelha', 5000, 100, '🐑'), ('Coelho', 10000, 200, '🐇'),
               ('Cabra', 15000, 300, '🐐'), ('Cavalo', 20000, 400, '🐎')]
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


# ========= XRocket Webhook ==========
@app.post("/webhook/xrocket")
async def xrocket_webhook(request: Request):
    data = await request.json()
    if data.get('status') == 'paid':
        try:
            user_id = int(data.get('payload'))
            ton_value = float(data.get('amount'))
            cash_value = int(ton_value * 1000)
            cur.execute(
                "UPDATE usuarios SET saldo_cash = saldo_cash + ? WHERE telegram_id = ?",
                (cash_value, user_id))
            con.commit()
            # Notifica o usuário no Telegram
            await bot.send_message(
                user_id,
                f"✅ Depósito confirmado!\n💰 {cash_value} cash foram creditados na sua conta."
            )
        except Exception as e:
            print('Erro ao processar depósito:', e)
    return {"ok": True}


# ========= MENU PRINCIPAL ==========
def menu():
    return types.ReplyKeyboardMarkup(
        keyboard=[[
            types.KeyboardButton(text="🐾 Meus Animais"),
            types.KeyboardButton(text="💰 Meu Saldo")
        ],
                  [
                      types.KeyboardButton(text="🛒 Comprar"),
                      types.KeyboardButton(text="➕ Depositar")
                  ],
                  [
                      types.KeyboardButton(text="🔄 Trocar cash por TON"),
                      types.KeyboardButton(text="🏦 Sacar")
                  ],
                  [
                      types.KeyboardButton(text="👫 Indique & Ganhe"),
                      types.KeyboardButton(text="❓ Ajuda/Suporte")
                  ]],
        resize_keyboard=True)


# ========= HANDLERS ==========
@dp.message(Command('start'))
async def start(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute(
        "INSERT OR IGNORE INTO usuarios (telegram_id, criado_em) VALUES (?, ?)",
        (user_id, datetime.now().isoformat()))
    con.commit()
    cur.execute(
        "SELECT saldo_cash, saldo_ton FROM usuarios WHERE telegram_id=?",
        (user_id, ))
    saldo_cash, saldo_ton = cur.fetchone() or (0, 0)
    cur.execute("SELECT SUM(quantidade) FROM inventario WHERE telegram_id=?",
                (user_id, ))
    total_animais = cur.fetchone()[0] or 0
    cur.execute(
        "SELECT SUM(quantidade * rendimento) FROM inventario JOIN animais ON inventario.animal = animais.nome WHERE inventario.telegram_id=?",
        (user_id, ))
    rendimento_dia = cur.fetchone()[0] or 0
    texto = (
        f"🌾 *Bem-vindo à Fazenda TON!*\n\n"
        f"💸 Cash: `{saldo_cash:.0f}` | 💎 TON: `{saldo_ton:.4f}`\n"
        f"🐾 Animais: `{total_animais}` | 📈 Rendimento/dia: `{rendimento_dia:.2f} cash`\n\n"
        "Escolha uma opção:")
    await msg.answer(texto, reply_markup=menu(), parse_mode="Markdown")


@dp.message(lambda msg: msg.text == "💰 Meu Saldo")
async def saldo(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute(
        "SELECT saldo_cash, saldo_ton FROM usuarios WHERE telegram_id=?",
        (user_id, ))
    saldo_cash, saldo_ton = cur.fetchone() or (0, 0)
    await msg.answer(
        f"💸 Seu saldo em cash: `{saldo_cash:.0f}`\n"
        f"💎 Seu saldo em TON: `{saldo_ton:.4f}`\n"
        f"Cada 1 TON = 1000 cash",
        parse_mode="Markdown")


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
    await msg.answer(texto,
                     reply_markup=types.ReplyKeyboardMarkup(
                         keyboard=keyboard, resize_keyboard=True),
                     parse_mode="Markdown")


@dp.message(lambda msg: msg.text and msg.text.startswith(
    tuple(['🐔', '🐖', '🐄', '🐂', '🐑', '🐇', '🐐', '🐎'])) and "Comprar" in msg.text)
async def comprar_animal(msg: types.Message):
    user_id = msg.from_user.id
    nome = msg.text.split("Comprar ")[1]
    cur.execute("SELECT preco FROM animais WHERE nome=?", (nome, ))
    r = cur.fetchone()
    if not r:
        await msg.answer("Animal não encontrado.")
        return
    preco = r[0]
    cur.execute("SELECT saldo_cash FROM usuarios WHERE telegram_id=?",
                (user_id, ))
    saldo = cur.fetchone()[0] if cur.fetchone() else 0
    if saldo < preco:
        await msg.answer("❌ Saldo insuficiente.")
        return
    cur.execute(
        "UPDATE usuarios SET saldo_cash=saldo_cash-? WHERE telegram_id=?",
        (preco, user_id))
    cur.execute(
        "INSERT OR IGNORE INTO inventario (telegram_id, animal, quantidade, ultima_coleta) VALUES (?, ?, 0, ?)",
        (user_id, nome, datetime.now().isoformat()))
    cur.execute(
        "UPDATE inventario SET quantidade=quantidade+1, ultima_coleta=? WHERE telegram_id=? AND animal=?",
        (datetime.now().isoformat(), user_id, nome))
    con.commit()
    await msg.answer(f"Parabéns! Você comprou um(a) {nome} 🎉",
                     reply_markup=menu())


@dp.message(lambda msg: msg.text == "⬅️ Voltar")
async def voltar(msg: types.Message):
    await start(msg)


@dp.message(lambda msg: msg.text == "🐾 Meus Animais")
async def meus_animais(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute(
        "SELECT animal, quantidade FROM inventario WHERE telegram_id=?",
        (user_id, ))
    itens = cur.fetchall()
    if not itens:
        await msg.answer("Você ainda não possui animais. Compre um na loja!")
        return
    resposta = "🐾 *Seus Animais:*\n\n"
    total_rendimento = 0
    for animal, qtd in itens:
        cur.execute("SELECT rendimento, emoji FROM animais WHERE nome=?",
                    (animal, ))
        rendimento, emoji = cur.fetchone()
        resposta += f"{emoji} {animal}: `{qtd}` | Rendimento: `{rendimento * qtd:.1f} cash/dia`\n"
        total_rendimento += rendimento * qtd
    resposta += f"\n📈 *Total rendimento/dia:* `{total_rendimento:.1f} cash`"
    await msg.answer(resposta, parse_mode="Markdown")


@dp.message(lambda msg: msg.text == "➕ Depositar")
async def depositar(msg: types.Message):
    kb = types.ReplyKeyboardMarkup(
        keyboard=[[
            types.KeyboardButton(text="0.1 TON"),
            types.KeyboardButton(text="0.5 TON")
        ],
                  [
                      types.KeyboardButton(text="1 TON"),
                      types.KeyboardButton(text="2.5 TON")
                  ],
                  [
                      types.KeyboardButton(text="5 TON"),
                      types.KeyboardButton(text="10 TON")
                  ],
                  [
                      types.KeyboardButton(text="15 TON"),
                      types.KeyboardButton(text="Outro valor")
                  ], [types.KeyboardButton(text="⬅️ Voltar")]],
        resize_keyboard=True)
    await msg.answer("Escolha o valor do depósito:", reply_markup=kb)


def criar_fatura_xrocket(user_id, valor_ton):
    import requests
    url = "https://xrocket.tg/api/v1/invoice/create"
    data = {
        "api_key": XROCKET_API_TOKEN,
        "currency": "TON",
        "amount": valor_ton,
        "comment": f"Depósito para usuário {user_id}",
        "payload": str(user_id),
        "lifetime": 3600
    }
    try:
        resp = requests.post(url, json=data)
        r = resp.json()
        if "pay_url" in r:
            return r["pay_url"]
        else:
            print(r)
            return None
    except Exception as e:
        print("Erro ao criar fatura xRocket:", e)
        return None


@dp.message(
    lambda msg: msg.text in
    ["0.1 TON", "0.5 TON", "1 TON", "2.5 TON", "5 TON", "10 TON", "15 TON"])
async def gerar_fatura_padrao(msg: types.Message):
    valores = {
        "0.1 TON": 0.1,
        "0.5 TON": 0.5,
        "1 TON": 1,
        "2.5 TON": 2.5,
        "5 TON": 5,
        "10 TON": 10,
        "15 TON": 15
    }
    valor = valores[msg.text]
    link_pagamento = criar_fatura_xrocket(msg.from_user.id, valor)
    if link_pagamento:
        await msg.answer(
            f"Para depositar {valor} TON, pague via xRocket:\n\n{link_pagamento}"
        )
    else:
        await msg.answer("Erro ao gerar cobrança. Tente novamente.")


@dp.message(lambda msg: msg.text == "Outro valor")
async def outro_valor(msg: types.Message):
    await msg.answer(
        "Envie o valor desejado em TON (apenas o número). Exemplo: 3.2")


@dp.message(lambda msg: msg.text.replace('.', '', 1).isdigit())
async def gerar_fatura_custom(msg: types.Message):
    try:
        valor = float(msg.text)
        if valor < 0.1:
            await msg.answer("O valor mínimo é 0.1 TON.")
            return
        link_pagamento = criar_fatura_xrocket(msg.from_user.id, valor)
        if link_pagamento:
            await msg.answer(
                f"Para depositar {valor} TON, pague via xRocket:\n\n{link_pagamento}"
            )
        else:
            await msg.answer("Erro ao gerar cobrança. Tente novamente.")
    except Exception:
        await msg.answer("Valor inválido. Tente novamente.")


@dp.message(lambda msg: msg.text == "🔄 Trocar cash por TON")
async def trocar_cash(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute(
        "SELECT saldo_cash, saldo_ton FROM usuarios WHERE telegram_id=?",
        (user_id, ))
    saldo_cash, saldo_ton = cur.fetchone()
    if saldo_cash < 1000:
        await msg.answer(
            f"Você precisa de pelo menos 1000 cash para trocar por TON.\nCada 1000 cash = 1 TON"
        )
        return
    ton_adicionado = saldo_cash // 1000
    novo_cash = saldo_cash % 1000
    novo_ton = saldo_ton + ton_adicionado
    cur.execute(
        "UPDATE usuarios SET saldo_cash=?, saldo_ton=? WHERE telegram_id=?",
        (novo_cash, novo_ton, user_id))
    con.commit()
    await msg.answer(
        f"🔄 Troca realizada!\nAgora você tem `{novo_cash:.0f}` cash e `{novo_ton:.4f}` TON.\n(Use o menu principal para sacar)"
    )


@dp.message(lambda msg: msg.text == "🏦 Sacar")
async def sacar(msg: types.Message):
    await msg.answer(
        "Envie o valor em TON e a carteira TON para sacar. Exemplo:\n`2 UQxxxxxxxxxxxx`",
        parse_mode="Markdown")


@dp.message(lambda msg: msg.text == "👫 Indique & Ganhe")
async def indicacao(msg: types.Message):
    user_id = msg.from_user.id
    link = f"https://t.me/seu_bot?start={user_id}"
    await msg.answer(f"Convide amigos com este link e ganhe bônus:\n{link}")


@dp.message(lambda msg: msg.text == "❓ Ajuda/Suporte")
async def ajuda(msg: types.Message):
    await msg.answer(
        "Dúvidas? Fale com o suporte: @seu_suporte\n\n• 🛒 Comprar animais com cash\n• 💰 Depositar via TON\n• 🔄 Trocar cash por TON\n• 🏦 Sacar TON para sua carteira\n\nQualquer dúvida, fale conosco!"
    )


@dp.message(lambda msg: msg.text and msg.text.split(' ')[0].replace(
    '.', '', 1).isdigit())
async def processa_saque(msg: types.Message):
    try:
        partes = msg.text.strip().split(' ')
        valor = float(partes[0])
        carteira = partes[1]
    except Exception:
        await msg.answer("Formato inválido. Envie: VALOR CARTEIRA_TON")
        return
    user_id = msg.from_user.id
    cur.execute("SELECT saldo_ton FROM usuarios WHERE telegram_id=?",
                (user_id, ))
    saldo = cur.fetchone()[0]
    if saldo < valor:
        await msg.answer("Saldo insuficiente.")
        return
    cur.execute(
        "UPDATE usuarios SET saldo_ton=saldo_ton-? WHERE telegram_id=?",
        (valor, user_id))
    con.commit()
    await msg.answer(
        f"Saque de {valor:.4f} TON para a carteira {carteira} está sendo processado.\n\n(Simulação MVP)"
    )


# ========= INICIAR BOT ==========
def start_bot():
    asyncio.create_task(dp.start_polling(bot))


@app.on_event("startup")
async def on_startup():
    start_bot()


# ========== FASTAPI MAIN ==========
if __name__ == '__main__':
    uvicorn.run("fazenda_ton_bot.bot_main:app",
                host="0.0.0.0",
                port=8000,
                reload=True)
