import json
import sqlite3
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo
import asyncio

import discord
from discord.ext import tasks

# =========================
# CONFIGURACIÓN HARDCODEADA
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")  # CAMBIA ESTO
CHANNEL_ID = 1472720385618477271

DB_PATH = "problems.db"
JSON_PATH = "problems.json"

TZ = ZoneInfo("Atlantic/Canary")
SEND_TIME = time(hour=23, minute=20, tzinfo=TZ)

# =========================
# BASE DE DATOS
# =========================
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    """Crea la tabla (con source) si no existe."""
    with db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS problems (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                latex TEXT NOT NULL,
                source TEXT,
                used INTEGER NOT NULL DEFAULT 0,
                added_at TEXT NOT NULL
            )
            """
        )
        con.commit()

def import_json(json_path: str):
    """
    Añade a la base de datos TODOS los problemas del JSON que aún no estén
    (mismo latex+source), manteniendo el orden por added_at.
    Si el JSON está vacío o no existe, no añade nada.
    """
    if not os.path.exists(json_path):
        print(f"No se encontró {json_path}")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        try:
            items = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error al leer {json_path}: {e}")
            return

    if not items:
        print("problems.json está vacío, no se añaden problemas.")
        return

    added = 0
    with db() as con:
        for it in items:
            latex = str(it["latex"]).strip()
            source = str(it.get("source", "")).strip()
            added_at = datetime.now(tz=TZ).isoformat()

            # Evitar duplicados exactos de latex + source
            cur = con.execute(
                "SELECT COUNT(*) FROM problems WHERE latex = ? AND source = ?",
                (latex, source),
            )
            exists = cur.fetchone()[0]
            if exists:
                continue

            con.execute(
                "INSERT INTO problems(latex, source, used, added_at) "
                "VALUES (?, ?, 0, ?)",
                (latex, source, added_at),
            )
            added += 1
        con.commit()

    print(f"Importados {added} problemas nuevos desde {json_path}")

def pick_next_problem():
    """
    Escoge el siguiente problema NO usado, en orden de inserción (id creciente).
    No resetea jamás el 'used': cada problema se usa como máximo una vez.
    """
    with db() as con:
        row = con.execute(
            "SELECT id, latex, source FROM problems "
            "WHERE used = 0 ORDER BY id ASC LIMIT 1"
        ).fetchone()

        if row is None:
            return None

        pid, latex, source = row
        con.execute("UPDATE problems SET used = 1 WHERE id = ?", (pid,))
        con.commit()
        return pid, latex, source

def remaining_problems_count():
    with db() as con:
        cur = con.execute("SELECT COUNT(*) FROM problems WHERE used = 0")
        return cur.fetchone()[0]

def total_problems_count():
    with db() as con:
        cur = con.execute("SELECT COUNT(*) FROM problems")
        return cur.fetchone()[0]

def used_problems_count():
    with db() as con:
        cur = con.execute("SELECT COUNT(*) FROM problems WHERE used = 1")
        return cur.fetchone()[0]

# =========================
# BOT
# =========================
class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)

    async def setup_hook(self):
        daily_problem.start()

bot = Bot()

@bot.event
async def on_ready():
    print(f"Conectado como {bot.user} (id={bot.user.id})")
    print(f"Canal objetivo: {CHANNEL_ID}")
    print(f"Hora diaria (Canarias): {SEND_TIME}")

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(CHANNEL_ID)

    total = total_problems_count()
    usados = used_problems_count()
    restantes = remaining_problems_count()
    numero_siguiente = usados + 1  # número de problema por el que va

    mensaje_info = (
        f"📊 Problemas en la base de datos: {total}\n"
        f"✅ Ya enviados: {usados}\n"
        f"🕒 Pendientes: {restantes}\n"
        f"➡️ Próximo problema: #{numero_siguiente}"
    )

    await channel.send(mensaje_info)

@tasks.loop(time=SEND_TIME)
async def daily_problem():
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(CHANNEL_ID)

    # Intentamos importar nuevos problemas del JSON cada día
    import_json(JSON_PATH)

    restantes = remaining_problems_count()
    if restantes == 0:
        await channel.send("❌ Faltan problemas en la base de datos.")
        return

    picked = pick_next_problem()
    if picked is None:
        await channel.send("❌ Faltan problemas en la base de datos.")
        return

    pid, latex, source = picked
    numero_problema = used_problems_count()  # ya incluye el que acabamos de marcar

    mensaje = f"```latex\n{latex}\n```"
    if source:
        fuente_msg = f"Fuente || {source} ||"
    else:
        fuente_msg = "Fuente || [fuente no especificada] ||"

    encabezado = f"📌 Problema #{numero_problema}"
    print("VOY A ENVIAR:", repr(encabezado), repr(mensaje), repr(fuente_msg))

    await channel.send(encabezado)
    await channel.send(mensaje)
    await channel.send(fuente_msg)

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Falta DISCORD_TOKEN (TOKEN está vacío).")

    init_db()
    # Importamos posibles problemas nuevos al arrancar
    import_json(JSON_PATH)

    bot.run(TOKEN)
