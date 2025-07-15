import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import sqlite3
from datetime import datetime
import requests

# ========= CONFIGURAÇÕES ==========
TOKEN = os.getenv('8164159394:AAEEiiHJEOWMxjqlEloJSy4E1Aswt7gXZlE')
XROCKET_API_TOKEN = os.getenv('b4f7d600e6eef6cddad455eac')

# ========= BANCO DE DADOS =========
con = sqlite3.connect('fazenda.db')
cur = con.cursor()

def init_db():
    cur.execute('''CREATE TABLE IF NOT EXISTS usuarios (
        telegram_id INTEGER PRIMARY KEY,
        saldo REAL DEFAULT 0,
        saldo_pendente REAL DEFAULT 0,
        carteira_ton TEXT,
        indicado_por INTEGER,
        ganhos_indicacao REAL DEFAULT 0,
        criado_em TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS animais (
        nome TEXT PRIMARY KEY,
        preco REAL,
        rendimento REAL
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
        ('Galinha', 1, 0.05),
        ('Vaca', 3, 0.20),
        ('Boi', 5, 0.40),
    ]
    for nome, preco, rendimento in animais:
        cur.execute("INSERT OR IGNORE INTO animais (nome, preco, rendimento) VALUES (?, ?, ?)", (nome, preco, rendimento))
    con.commit()

init_db()
cadastrar_animais()

# ========= BOT ==========
bot = Bot(token=TOKEN)
dp = Dispatcher()

def menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row('🐔 Meus Animais', '💰 Meu Saldo')
    kb.row('🛒 Comprar', '➕ Depositar')
    kb.row('🏦 Sacar', '👫 Indique & Ganhe')
    kb.row('❓ Ajuda/Suporte')
    return kb

# ========== XROCKET INTEGRAÇÃO ==========
def criar_fatura_xrocket(user_id, valor_ton):
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

# ========== HANDLERS ==========

@dp.message(Command('start'))
async def start(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute("INSERT OR IGNORE INTO usuarios (telegram_id, criado_em) VALUES (?, ?)", (user_id, datetime.now().isoformat()))
    con.commit()
    texto = (
        "🐮 *Bem-vindo à Fazenda TON!*

"
        "Invista em animais de fazenda que rendem TON para você.

"
        "Escolha uma opção abaixo para começar:"
    )
    await msg.answer(texto, reply_markup=menu(), parse_mode="Markdown")

@dp.message(lambda msg: msg.text == '💰 Meu Saldo')
async def saldo(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute("SELECT saldo, saldo_pendente FROM usuarios WHERE telegram_id=?", (user_id,))
    r = cur.fetchone()
    saldo, saldo_pendente = r if r else (0, 0)
    await msg.answer(f"💰 *Seu saldo disponível:* `{saldo:.4f} TON`
🪙 *Rendimentos a coletar:* `{saldo_pendente:.4f} TON`", parse_mode="Markdown")

@dp.message(lambda msg: msg.text == '➕ Depositar')
async def depositar(msg: types.Message):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row('1 TON', '5 TON')
    kb.row('10 TON', 'Outro valor')
    kb.row('Menu')
    await msg.answer(
        "Escolha o valor para depósito, ou envie o valor desejado (ex: 2.5 TON):",
        reply_markup=kb
    )

@dp.message(lambda msg: msg.text in ['1 TON', '5 TON', '10 TON'])
async def gerar_fatura_padrao(msg: types.Message):
    valores = {'1 TON': 1, '5 TON': 5, '10 TON': 10}
    valor = valores[msg.text]
    link_pagamento = criar_fatura_xrocket(msg.from_user.id, valor)
    if link_pagamento:
        await msg.answer(f"Clique no link para pagar {valor} TON via xRocket:
{link_pagamento}")
    else:
        await msg.answer("Erro ao gerar cobrança. Tente novamente.")

@dp.message(lambda msg: msg.text == 'Outro valor')
async def outro_valor(msg: types.Message):
    await msg.answer("Envie o valor desejado em TON, somente o número. Ex: 2.5")

@dp.message(lambda msg: msg.text.replace('.', '', 1).isdigit())
async def gerar_fatura_custom(msg: types.Message):
    try:
        valor = float(msg.text)
        if valor < 0.1:
            await msg.answer("O valor mínimo é 0.1 TON.")
            return
        link_pagamento = criar_fatura_xrocket(msg.from_user.id, valor)
        if link_pagamento:
            await msg.answer(f"Clique no link para pagar {valor} TON via xRocket:
{link_pagamento}")
        else:
            await msg.answer("Erro ao gerar cobrança. Tente novamente.")
    except Exception:
        await msg.answer("Valor inválido. Tente novamente.")

@dp.message(lambda msg: msg.text == 'Menu')
async def voltar_menu(msg: types.Message):
    await msg.answer("Menu principal:", reply_markup=menu())

@dp.message(lambda msg: msg.text == '🛒 Comprar')
async def comprar(msg: types.Message):
    cur.execute("SELECT nome, preco, rendimento FROM animais")
    animais = cur.fetchall()
    txt = "Escolha o animal que deseja comprar, respondendo com o nome:

"
    for nome, preco, rendimento in animais:
        txt += f"🐾 *{nome}* — `{preco} TON` | Rende `{rendimento} TON/dia`
"
    await msg.answer(txt, parse_mode="Markdown")

@dp.message(lambda msg: msg.text in ['Galinha', 'Vaca', 'Boi'])
async def efetuar_compra(msg: types.Message):
    user_id = msg.from_user.id
    animal = msg.text
    cur.execute("SELECT preco FROM animais WHERE nome=?", (animal,))
    r = cur.fetchone()
    if not r:
        await msg.answer("Animal não encontrado.")
        return
    preco = r[0]
    cur.execute("SELECT saldo FROM usuarios WHERE telegram_id=?", (user_id,))
    r = cur.fetchone()
    saldo = r[0] if r else 0
    if saldo < preco:
        await msg.answer("Saldo insuficiente.")
        return
    cur.execute("UPDATE usuarios SET saldo=saldo-? WHERE telegram_id=?", (preco, user_id))
    cur.execute("INSERT OR IGNORE INTO inventario (telegram_id, animal, quantidade, ultima_coleta) VALUES (?, ?, 0, ?)",
                (user_id, animal, datetime.now().isoformat()))
    cur.execute("UPDATE inventario SET quantidade=quantidade+1 WHERE telegram_id=? AND animal=?", (user_id, animal))
    con.commit()
    await msg.answer(f"Parabéns! Você comprou uma {animal}. Agora ela irá render TON diariamente!")

@dp.message(lambda msg: msg.text == '🐔 Meus Animais')
async def meus_animais(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute("SELECT animal, quantidade, ultima_coleta FROM inventario WHERE telegram_id=?", (user_id,))
    itens = cur.fetchall()
    if not itens:
        await msg.answer("Você ainda não possui animais. Compre um na loja!")
        return
    resposta = "🐾 *Seus Animais de Fazenda:*

"
    total_rendimento = 0
    for animal, qtd, ultima_coleta in itens:
        cur.execute("SELECT rendimento FROM animais WHERE nome=?", (animal,))
        rendimento = cur.fetchone()[0]
        dias = (datetime.now() - datetime.fromisoformat(ultima_coleta)).total_seconds() // (60*60*24)
        dias = max(1, int(dias))
        rendimento_acumulado = rendimento * qtd * dias
        resposta += f"{animal}: {qtd} | Rendimentos: `{rendimento_acumulado:.4f} TON`
"
        cur.execute("UPDATE usuarios SET saldo_pendente=saldo_pendente+? WHERE telegram_id=?", (rendimento_acumulado, user_id))
        cur.execute("UPDATE inventario SET ultima_coleta=? WHERE telegram_id=? AND animal=?", (datetime.now().isoformat(), user_id, animal))
        total_rendimento += rendimento_acumulado
    con.commit()
    resposta += f"\nTotal coletado agora: `{total_rendimento:.4f} TON`"
    await msg.answer(resposta, parse_mode="Markdown")

@dp.message(lambda msg: msg.text == '🏦 Sacar')
async def sacar(msg: types.Message):
    await msg.answer("Informe o valor e a carteira TON para sacar. Exemplo:\n\n`10 UQxxxxxxxxxxxx`", parse_mode="Markdown")

@dp.message(lambda msg: msg.text == '👫 Indique & Ganhe')
async def indicacao(msg: types.Message):
    user_id = msg.from_user.id
    link = f"https://t.me/seu_bot?start={user_id}"
    await msg.answer(f"Convide amigos com este link e ganhe bônus:\n{link}")

@dp.message(lambda msg: msg.text == '❓ Ajuda/Suporte')
async def ajuda(msg: types.Message):
    await msg.answer("Dúvidas? Fale com o suporte: @seu_suporte\n\nDica: para sacar, clique em 🏦 Sacar e siga as instruções!")

@dp.message(lambda msg: msg.text and msg.text.split(' ')[0].replace('.', '', 1).isdigit())
async def processa_saque(msg: types.Message):
    try:
        partes = msg.text.strip().split(' ')
        valor = float(partes[0])
        carteira = partes[1]
    except Exception:
        await msg.answer("Formato inválido. Envie: VALOR CARTEIRA_TON")
        return
    user_id = msg.from_user.id
    cur.execute("SELECT saldo FROM usuarios WHERE telegram_id=?", (user_id,))
    saldo = cur.fetchone()[0]
    if saldo < valor:
        await msg.answer("Saldo insuficiente.")
        return
    cur.execute("UPDATE usuarios SET saldo=saldo-? WHERE telegram_id=?", (valor, user_id))
    con.commit()
    await msg.answer(f"Saque de {valor:.4f} TON para a carteira {carteira} está sendo processado.\n\n(Simulação MVP)")

@dp.message(lambda msg: msg.text.lower() == 'coletar')
async def coletar(msg: types.Message):
    user_id = msg.from_user.id
    cur.execute("SELECT saldo_pendente FROM usuarios WHERE telegram_id=?", (user_id,))
    valor = cur.fetchone()[0]
    if valor > 0:
        cur.execute("UPDATE usuarios SET saldo=saldo+?, saldo_pendente=0 WHERE telegram_id=?", (valor, user_id))
        con.commit()
        await msg.answer(f"Você coletou {valor:.4f} TON em rendimentos! Saldo atualizado.")
    else:
        await msg.answer("Nenhum rendimento pendente para coletar.")

# ========== MAIN ==========
async def main():
    print('Bot rodando...')
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())