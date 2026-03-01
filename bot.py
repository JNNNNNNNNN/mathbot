import json
import sqlite3
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks

# =========================
# CONFIGURACIÓN HARDCODEADA
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")

INFO_CHANNEL_ID = 1472747495162380481      # canal donde se envía el resumen
PROBLEM_CHANNEL_ID = 1472720385618477271   # canal donde se envían los problemas
HORA_CHANNEL_ID = 1477113428257673246      # canal donde se envía el embed de !hora

DB_PATH = "problems.db"
JSON_PATH = "problems.json"

TZ = ZoneInfo("Atlantic/Canary")
SEND_TIME = time(hour=21, minute=30, tzinfo=TZ)

# Reacciones para el cuestionario de hora (inicio)
HORA_REACTIONS = ["1️⃣", "2️⃣", "3️⃣"]  # 11, 12, 13

# =========================
# BASE DE DATOS
# =========================
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    """Crea la tabla (con source y skip_offset) si no existe."""
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
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            )
            """
        )
        cur = con.execute("SELECT value FROM meta WHERE key = 'skip_offset'")
        row = cur.fetchone()
        if row is None:
            con.execute("INSERT INTO meta(key, value) VALUES('skip_offset', 0)")
        con.commit()

def get_skip_offset() -> int:
    with db() as con:
        cur = con.execute("SELECT value FROM meta WHERE key = 'skip_offset'")
        row = cur.fetchone()
        return int(row[0]) if row else 0

def set_skip_offset(value: int):
    with db() as con:
        con.execute(
            "UPDATE meta SET value = ? WHERE key = 'skip_offset'",
            (int(value),),
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

            cur = con.execute(
                "SELECT COUNT(*) FROM problems WHERE latex = ? AND source = ?",
                (latex, source),
            )
            exists = cur.fetchone()[0]
            if exists:
                continue

            con.execute(
                "INSERT INTO problems(latex, source, used, added_at) VALUES (?, ?, 0, ?)",
                (latex, source, added_at),
            )
            added += 1
        con.commit()

    print(f"Importados {added} problemas nuevos desde {json_path}")

def total_problems_count():
    with db() as con:
        cur = con.execute("SELECT COUNT(*) FROM problems")
        return cur.fetchone()[0]

def used_problems_count():
    with db() as con:
        cur = con.execute("SELECT COUNT(*) FROM problems WHERE used = 1")
        return cur.fetchone()[0]

def remaining_problems_count():
    with db() as con:
        cur = con.execute("SELECT COUNT(*) FROM problems WHERE used = 0")
        return cur.fetchone()[0]

def get_problem_by_index(idx: int):
    """
    Devuelve el problema por índice lógico 1-based (ordenado por id ASC),
    sin tocar 'used'.
    """
    if idx <= 0:
        return None

    with db() as con:
        row = con.execute(
            "SELECT id, latex, source FROM problems ORDER BY id ASC LIMIT 1 OFFSET ?",
            (idx - 1,),
        ).fetchone()

        if row is None:
            return None

        pid, latex, source = row
        return pid, latex, source

def mark_used(pid: int):
    with db() as con:
        con.execute("UPDATE problems SET used = 1 WHERE id = ?", (pid,))
        con.commit()

def pick_next_with_skip():
    """
    Escoge el problema del día teniendo en cuenta:
      - cuántos ya se han usado (usados),
      - el offset global de skip (skip_offset).
    Índice lógico = usados + 1 + skip_offset.
    Marca used = 1 para ese problema.
    """
    usados = used_problems_count()
    skip = get_skip_offset()
    total = total_problems_count()

    logical_index = usados + 1 + skip
    if logical_index > total:
        return None

    result = get_problem_by_index(logical_index)
    if result is None:
        return None

    pid, latex, source = result
    mark_used(pid)
    return logical_index, latex, source

# =========================
# BOT
# =========================
class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # necesario para leer !skip y !hora
        intents.members = False
        intents.presences = False
        super().__init__(intents=intents)

        # Guardamos el último mensaje de encuesta !hora para validación
        self.hora_poll_message_id = None
        self.hora_poll_channel_id = HORA_CHANNEL_ID

    async def setup_hook(self):
        daily_problem.start()

bot = Bot()

@bot.event
async def on_ready():
    print(f"Conectado como {bot.user} (id={bot.user.id})")
    print(f"Canal de info: {INFO_CHANNEL_ID}")
    print(f"Canal de problemas: {PROBLEM_CHANNEL_ID}")
    print(f"Canal de !hora: {HORA_CHANNEL_ID}")
    print(f"Hora diaria (Canarias): {SEND_TIME}")

    info_channel = bot.get_channel(INFO_CHANNEL_ID)
    if info_channel is None:
        info_channel = await bot.fetch_channel(INFO_CHANNEL_ID)

    total = total_problems_count()
    usados = used_problems_count()
    restantes = remaining_problems_count()
    skip = get_skip_offset()
    numero_siguiente_logico = usados + 1 + skip

    mensaje_info = (
        f"📊 Problemas en la base de datos: {total}\n"
        f"✅ Ya enviados (marcados como usados): {usados}\n"
        f"🕒 Pendientes (unused): {restantes}\n"
        f"⏭ Offset de skip actual: {skip}\n"
        f"➡️ Próximo problema lógico (con skip): #{numero_siguiente_logico}"
    )

    await info_channel.send(mensaje_info)

async def send_hora_poll(trigger_channel: discord.abc.Messageable, author: discord.User):
    """
    Envía un embed con encuesta al canal HORA_CHANNEL_ID y añade 3 reacciones.
    """
    channel = bot.get_channel(HORA_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(HORA_CHANNEL_ID)

    embed = discord.Embed(
        title="🗓️ Próxima reunión: elige hora de inicio",
        description=(
            "¿A qué hora quieres que empiece?\n\n"
            "1️⃣ 11:00\n"
            "2️⃣ 12:00\n"
            "3️⃣ 13:00\n\n"
            "Reacciona abajo con tu opción."
        ),
        color=0x5865F2,
        timestamp=datetime.now(tz=TZ),
    )
    embed.set_footer(text=f"Solicitado por: {author}")

    msg = await channel.send(embed=embed)
    bot.hora_poll_message_id = msg.id
    bot.hora_poll_channel_id = channel.id

    # Añadimos reacciones (unicode emojis)
    for e in HORA_REACTIONS:
        await msg.add_reaction(e)  # unicode o custom emoji válido [web:2]

    # Confirmación opcional en el canal donde se invoca
    if trigger_channel and hasattr(trigger_channel, "send") and trigger_channel != channel:
        await trigger_channel.send(f"✅ Encuesta enviada en <#{channel.id}>.")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # Comando: !skip <n>
    if message.content.startswith("!skip"):
        parts = message.content.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.channel.send("Uso: `!skip <número_de_problema_que_quieres_para_hoy>`")
            return

        n = int(parts[1])
        total = total_problems_count()
        if total == 0:
            await message.channel.send("No hay problemas en la base de datos.")
            return

        if n <= 0 or n > total:
            await message.channel.send(f"El número debe estar entre 1 y {total}.")
            return

        usados = used_problems_count()
        nuevo_skip = n - (usados + 1)
        if nuevo_skip < 0:
            await message.channel.send(
                f"El problema #{n} ya está por detrás del progreso actual (usados = {usados})."
            )
            return

        set_skip_offset(nuevo_skip)

        await message.channel.send(
            f"✅ Hoy se configuró para enviar el problema #{n}.\n"
            f"(usados = {usados}, skip_offset = {nuevo_skip})"
        )
        return

    # Comando: !hora
    if message.content.strip() == "!hora":
        await send_hora_poll(message.channel, message.author)
        return

@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    """
    Valida reacciones: solo cuenta si es en el mensaje de encuesta y si está
    dentro de las 3 reacciones permitidas. [web:6]
    """
    if user.bot:
        return

    if bot.hora_poll_message_id is None:
        return

    if reaction.message.id != bot.hora_poll_message_id:
        return

    if reaction.message.channel.id != bot.hora_poll_channel_id:
        return

    if str(reaction.emoji) not in HORA_REACTIONS:
        # Si reaccionan con otra cosa, la quitamos (requiere permisos Manage Messages)
        try:
            await reaction.message.remove_reaction(reaction.emoji, user)
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass
        return

    # Opcional: si quieres que cada usuario solo pueda votar una opción,
    # quitamos sus otras reacciones permitidas en ese mismo mensaje.
    try:
        for e in HORA_REACTIONS:
            if e != str(reaction.emoji):
                await reaction.message.remove_reaction(e, user)
    except discord.Forbidden:
        pass
    except discord.HTTPException:
        pass

@tasks.loop(time=SEND_TIME)
async def daily_problem():
    problem_channel = bot.get_channel(PROBLEM_CHANNEL_ID)
    if problem_channel is None:
        problem_channel = await bot.fetch_channel(PROBLEM_CHANNEL_ID)

    import_json(JSON_PATH)

    total = total_problems_count()
    if total == 0:
        await problem_channel.send("❌ No hay problemas en la base de datos.")
        return

    restantes = remaining_problems_count()
    if restantes == 0:
        await problem_channel.send("❌ Faltan problemas en la base de datos (todos usados).")
        return

    picked = pick_next_with_skip()
    if picked is None:
        await problem_channel.send("❌ No hay problema disponible con el skip actual.")
        return

    logical_index, latex, source = picked

    mensaje = f"```latex\n{latex}\n```"
    if source:
        fuente_msg = f"Fuente || {source} ||"
    else:
        fuente_msg = "Fuente || [fuente no especificada] ||"

    encabezado = f"📌 Problema #{logical_index}"
    print("VOY A ENVIAR:", repr(encabezado), repr(mensaje), repr(fuente_msg))

    await problem_channel.send(encabezado)
    await problem_channel.send(mensaje)
    await problem_channel.send(fuente_msg)

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("TOKEN está vacío.")

    init_db()
    import_json(JSON_PATH)

    bot.run(TOKEN)
