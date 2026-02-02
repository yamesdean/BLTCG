import os
import json
import time
import random
import aiosqlite
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv



load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
raw_gid = os.getenv("GUILD_ID")

try:
    GUILD_ID = int(raw_gid) if raw_gid else None
except ValueError:
    print(f"WARNING: GUILD_ID is not numeric: {raw_gid!r}. Falling back to global sync.")
    GUILD_ID = None

if not TOKEN:
    raise SystemExit("ERROR: DISCORD_TOKEN is missing")

DB_PATH = os.getenv("DB_PATH", "cards.db")
CARDS_JSON = os.getenv("CARDS_JSON", "cards.json")
LATEST_SET = os.getenv("LATEST_SET", "S2")          # Shop zieht IMMER aus dem neuesten Set
DEFAULT_ACTIVE_SET = os.getenv("DEFAULT_ACTIVE_SET", LATEST_SET)  # neue User starten hier
PULL_COOLDOWN_SECONDS = 5 * 60 * 60
DUPLICATE_COINS = int(os.getenv("DUPLICATE_COINS", "5"))

# ---- Rarity → Embed-Farbe -----------------------------------------------
def get_rarity_color(rarity: str) -> discord.Color:
    r = (rarity or "").strip().lower()
    if r == "legendary":
        return discord.Color.purple()
    if r == "ultra rare":
        return discord.Color.gold()
    if r == "rare":
        return discord.Color.fuchsia()
    # default (u. a. "common")
    return discord.Color.dark_gray()

# Seltenheits-Gewichte (anpassbar)
DEFAULT_WEIGHTS = {
    "Common": 75,
    "Rare": 25,
    "Ultra Rare": 3,
    "Legendary": 0.5
}

# ---------------- Bot Grundgerüst ----------------
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # DB vorbereiten + Karten laden
        await init_db()
        await load_cards_from_json()

        # Slash-Commands nur in deiner Guild registrieren (sofort sichtbar)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"✅ {len(synced)} Slash-Commands für Guild {GUILD_ID} synchronisiert")
        else:
            synced = await self.tree.sync()  # global (langsam)
            print(f"⏳ {len(synced)} globale Slash-Commands synchronisiert")

bot = MyBot()

@bot.event
async def on_ready():
    print(f"🚀 Eingeloggt als {bot.user} (ID: {bot.user.id})")

# Dekorator für Guild-Commands
guild_only = app_commands.guilds(discord.Object(id=GUILD_ID)) if GUILD_ID else (lambda f: f)

# ---------------- DB/Logic Helpers ----------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            rarity TEXT NOT NULL,
            set_id TEXT,
            image_url TEXT NOT NULL,
            flow INTEGER,
            punchlines INTEGER,
            style INTEGER,
            reputation INTEGER
        );

        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            last_pull_ts INTEGER DEFAULT 0,
            coins INTEGER DEFAULT 0,
            active_set TEXT
        );

        CREATE TABLE IF NOT EXISTS user_cards (
            user_id INTEGER NOT NULL,
            card_id TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, card_id),
            FOREIGN KEY (card_id) REFERENCES cards(id)
        );

        CREATE TABLE IF NOT EXISTS rarity_weights (
            rarity TEXT PRIMARY KEY,
            weight REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades (
            trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user INTEGER NOT NULL,
            to_user INTEGER NOT NULL,
            from_card_id TEXT NOT NULL,
            to_card_id TEXT NOT NULL,
            qty_from INTEGER NOT NULL DEFAULT 1,
            qty_to INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending',
            created_ts INTEGER NOT NULL
        );
        """)

        # ---- migrations (safe for existing DBs)

        # users table
        async with db.execute("PRAGMA table_info(users)") as cur:
            user_cols = [r[1] for r in await cur.fetchall()]

        if "coins" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN coins INTEGER DEFAULT 0")

        if "active_set" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN active_set TEXT")
            await db.execute(
                "UPDATE users SET active_set = ? WHERE active_set IS NULL",
                (DEFAULT_ACTIVE_SET,)
            )

        # cards table
        async with db.execute("PRAGMA table_info(cards)") as cur:
            card_cols = [r[1] for r in await cur.fetchall()]

        if "set_id" not in card_cols:
            await db.execute("ALTER TABLE cards ADD COLUMN set_id TEXT")
            await db.execute(
                "UPDATE cards SET set_id = 'S1' WHERE set_id IS NULL"
            )

        # rarity weights
        for r, w in DEFAULT_WEIGHTS.items():
            await db.execute(
                "INSERT INTO rarity_weights(rarity, weight) VALUES (?, ?) "
                "ON CONFLICT(rarity) DO UPDATE SET weight=excluded.weight",
                (r, w)
            )

        await db.commit()



# Karte nach exakter ID oder Name-Fragment finden
async def find_card_by_id_or_name(needle: str):
    needle_id = needle.strip()
    like = f"%{needle.strip()}%"
    query = """
    SELECT id, name, rarity, image_url
    FROM cards
    WHERE id = ? OR name LIKE ?
    ORDER BY (id = ?) DESC, name ASC
    LIMIT 1
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, (needle_id, like, needle_id)) as cur:
            return await cur.fetchone()  # (id, name, rarity, image_url) oder None

# Besitzerliste für eine bestimmte Karte (nach Menge sortiert)
async def get_owners_for_card(card_id: str, limit: int = 50):
    query = """
    SELECT user_id, qty
    FROM user_cards
    WHERE card_id = ? AND qty > 0
    ORDER BY qty DESC, user_id ASC
    LIMIT ?
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, (card_id, limit)) as cur:
            return await cur.fetchall()  # [(user_id, qty), ...]
from discord import app_commands

async def autocomplete_cards_by_name_or_id(interaction: discord.Interaction, current: str):
    like = f"%{(current or '').strip()}%"
    query = """
    SELECT id, name, rarity
    FROM cards
    WHERE id LIKE ? OR name LIKE ?
    ORDER BY 
      CASE rarity
        WHEN 'Legendary'  THEN 4
        WHEN 'Ultra Rare' THEN 3
        WHEN 'Rare'       THEN 2
        ELSE 1
      END DESC,
      name ASC
    LIMIT 25
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(query, (like, like)) as cur:
                rows = await cur.fetchall()
        # Label schön + value = ID (stabil)
        choices = []
        for cid, name, rarity in rows:
            label = f"{name} [{rarity}] — {cid}"
            choices.append(app_commands.Choice(name=label[:100], value=cid[:100]))
        return choices
    except Exception:
        return []



async def user_has_card_qty(user_id: int, card_id: str, qty: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT qty FROM user_cards WHERE user_id = ? AND card_id = ?", (user_id, card_id)) as cur:
            row = await cur.fetchone()
    return bool(row and row[0] >= qty)

async def transfer_card(user_from: int, user_to: int, card_id: str, qty: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_cards SET qty = qty - ? WHERE user_id = ? AND card_id = ? AND qty >= ?",
                         (qty, user_from, card_id, qty))
        await db.execute("DELETE FROM user_cards WHERE user_id = ? AND card_id = ? AND qty <= 0",
                         (user_from, card_id))
        await db.execute(
            "INSERT INTO user_cards(user_id, card_id, qty) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, card_id) DO UPDATE SET qty = qty + ?",
            (user_to, card_id, qty, qty)
        )
        await db.commit()

async def get_collection_leaderboard(limit: int = 10):
    """
    Liefert pro User:
      - score: gewichtete Punkte nach Rarity
      - cards_total: Gesamtanzahl Karten (Menge)
    Sortiert nach score DESC, dann cards_total DESC.
    """
    query = """
    SELECT
      uc.user_id AS user_id,
      SUM(uc.qty) AS cards_total,
      SUM(
        uc.qty * CASE c.rarity
          WHEN 'Legendary'  THEN 10
          WHEN 'Ultra Rare' THEN 5
          WHEN 'Rare'       THEN 2
          ELSE                   1
        END
      ) AS score
    FROM user_cards uc
    JOIN cards c ON c.id = uc.card_id
    GROUP BY uc.user_id
    ORDER BY score DESC, cards_total DESC
    LIMIT ?
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, (limit,)) as cur:
            rows = await cur.fetchall()
    # rows: [(user_id, cards_total, score), ...]
    return rows


async def get_cardcount_leaderboard(limit: int = 10):
    """
    Liefert pro User: Gesamtanzahl Karten (ohne Gewichtung).
    """
    query = """
    SELECT
      uc.user_id,
      SUM(uc.qty) AS cards_total
    FROM user_cards uc
    GROUP BY uc.user_id
    ORDER BY cards_total DESC
    LIMIT ?
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, (limit,)) as cur:
            rows = await cur.fetchall()
    return rows


class TradeView(discord.ui.View):
    def __init__(self, trade_id: int, from_user: int, to_user: int):
        super().__init__(timeout=120)
        self.trade_id = trade_id
        self.from_user = from_user
        self.to_user = to_user

    async def get_trade(self):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT trade_id, from_user, to_user, from_card_id, to_card_id, qty_from, qty_to, status FROM trades WHERE trade_id = ?", (self.trade_id,)) as cur:
                return await cur.fetchone()

    @discord.ui.button(label="✅ Annehmen", style=discord.ButtonStyle.success)
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade = await self.get_trade()
        if not trade: return await interaction.response.send_message("Trade nicht gefunden.", ephemeral=True)
        _, from_user, to_user, from_card_id, to_card_id, qty_from, qty_to, status = trade
        if interaction.user.id != to_user:
            return await interaction.response.send_message("Nur der Empfänger kann annehmen.", ephemeral=True)
        if status != "pending":
            return await interaction.response.send_message("Dieser Trade ist nicht mehr aktiv.", ephemeral=True)

        if not await user_has_card_qty(from_user, from_card_id, qty_from):
            return await interaction.response.send_message("Absender hat die Karte nicht mehr.", ephemeral=True)
        if not await user_has_card_qty(to_user, to_card_id, qty_to):
            return await interaction.response.send_message("Du hast die geforderte Karte nicht (mehr).", ephemeral=True)

        await transfer_card(from_user, to_user, from_card_id, qty_from)
        await transfer_card(to_user, from_user, to_card_id, qty_to)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE trades SET status='done' WHERE trade_id = ?", (self.trade_id,))
            await db.commit()

        await interaction.response.edit_message(content="✅ Trade abgeschlossen!", view=None)

    @discord.ui.button(label="⛔ Abbrechen", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade = await self.get_trade()
        if not trade: return await interaction.response.send_message("Trade nicht gefunden.", ephemeral=True)
        _, from_user, to_user, *_ = trade
        if interaction.user.id not in (from_user, to_user):
            return await interaction.response.send_message("Nur Beteiligte können abbrechen.", ephemeral=True)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE trades SET status='cancelled' WHERE trade_id = ?", (self.trade_id,))
            await db.commit()
        await interaction.response.edit_message(content="❌ Trade abgebrochen.", view=None)

@guild_only
@bot.tree.command(name="trade", description="Starte einen 1:1 Trade (mit Bestätigungs-Buttons).")
@app_commands.describe(user="Handelspartner", deine_karte="ID deiner Karte", seine_karte="ID der Karte des Partners",
                       deine_menge="Menge deiner Karte (default 1)", seine_menge="Menge seiner Karte (default 1)")
async def trade_start(interaction: discord.Interaction, user: discord.User, deine_karte: str, seine_karte: str, deine_menge: int = 1, seine_menge: int = 1):
    if user.id == interaction.user.id:
        return await interaction.response.send_message("Du kannst nicht mit dir selbst traden.", ephemeral=True)
    if not await user_has_card_qty(interaction.user.id, deine_karte, deine_menge):
        return await interaction.response.send_message("Du besitzt deine angebotene Karte nicht in ausreichender Menge.", ephemeral=True)
    if not await user_has_card_qty(user.id, seine_karte, seine_menge):
        return await interaction.response.send_message("Der Partner besitzt die geforderte Karte vermutlich nicht.", ephemeral=True)

    # Namen zur Anzeige
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM cards WHERE id = ?", (deine_karte,)) as cur:
            row1 = await cur.fetchone()
        async with db.execute("SELECT name FROM cards WHERE id = ?", (seine_karte,)) as cur:
            row2 = await cur.fetchone()
    name1 = row1[0] if row1 else deine_karte
    name2 = row2[0] if row2 else seine_karte

    # Trade anlegen
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO trades(from_user, to_user, from_card_id, to_card_id, qty_from, qty_to, created_ts)
            VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now'))
        """, (interaction.user.id, user.id, deine_karte, seine_karte, deine_menge, seine_menge))
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            trade_id = (await cur.fetchone())[0]

    view = TradeView(trade_id, interaction.user.id, user.id)
    content = (f"🤝 **Trade #{trade_id}**\n"
               f"{interaction.user.mention} bietet **{deine_menge}× {name1}** gegen **{seine_menge}× {name2}** von {user.mention}.\n"
               f"{user.mention}, bitte **annehmen** oder **abbrechen**.")
    await interaction.response.send_message(content, view=view)  # öffentlich ist hier ok


async def load_cards_from_json():
    if not os.path.exists(CARDS_JSON):
        print("⚠️ cards.json nicht gefunden – erst anlegen!")
        return
    with open(CARDS_JSON, "r", encoding="utf-8") as f:
        cards = json.load(f)

    async with aiosqlite.connect(DB_PATH) as db:
        for c in cards:
            stats = c.get("stats", {})
            set_id = c.get("set_id") or ("S2" if str(c["id"]).endswith("_s2") else "S1")
            await db.execute("""
                INSERT INTO cards(id, name, rarity, set_id, image_url, flow, punchlines, style, reputation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    rarity=excluded.rarity,
                    set_id=excluded.set_id,
                    image_url=excluded.image_url,
                    flow=excluded.flow,
                    punchlines=excluded.punchlines,
                    style=excluded.style,
                    reputation=excluded.reputation
            """, (
                c["id"], c["name"], c["rarity"], set_id, c["image_url"],
                stats.get("flow"), stats.get("punchlines"),
                stats.get("style"), stats.get("reputation")
            ))
        await db.commit()
    print(f"📥 {len(cards)} Karten aus cards.json geladen")

async def get_time_left(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT last_pull_ts FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    now = int(time.time())
    last_ts = row[0] if row else 0
    left = PULL_COOLDOWN_SECONDS - max(0, now - last_ts)
    return max(0, left)

async def mark_pulled(user_id: int):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id, last_pull_ts) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_pull_ts=excluded.last_pull_ts",
            (user_id, now)
        )
        await db.commit()

async def pick_rarity() -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT rarity, weight FROM rarity_weights") as cur:
            rows = await cur.fetchall()
    rarities = [r for (r, _) in rows]
    weights = [w for (_, w) in rows]
    return random.choices(rarities, weights=weights, k=1)[0]

async def pick_random_card_for_rarity(rarity: str, set_id: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if set_id:
            async with db.execute(
                "SELECT id, name, rarity, image_url, flow, punchlines, style, reputation "
                "FROM cards WHERE rarity = ? AND set_id = ?",
                (rarity, set_id)
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT id, name, rarity, image_url, flow, punchlines, style, reputation "
                "FROM cards WHERE rarity = ?",
                (rarity,)
            ) as cur:
                rows = await cur.fetchall()

    if not rows:
        return None
    return random.choice(rows)


async def add_coins(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id, coins) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET coins = COALESCE(coins,0) + ?",
            (user_id, amount, amount)
        )
        await db.commit()

async def get_coins(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0

async def get_active_set(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT active_set FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    if row and row[0]:
        return str(row[0])
    return DEFAULT_ACTIVE_SET

async def set_active_set(user_id: int, set_id: str):
    set_id = (set_id or "").strip().upper()
    if set_id not in ("S1", "S2"):
        raise ValueError("set_id must be S1 or S2")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id, active_set) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET active_set = excluded.active_set",
            (user_id, set_id)
        )
        await db.commit()


async def add_to_inventory(user_id: int, card_id: str) -> bool:
    """True, wenn Duplikat; sonst False"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT qty FROM user_cards WHERE user_id = ? AND card_id = ?", (user_id, card_id)) as cur:
            row = await cur.fetchone()
        duplicate = row is not None
        await db.execute(
            "INSERT INTO user_cards(user_id, card_id, qty) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, card_id) DO UPDATE SET qty = qty + 1",
            (user_id, card_id)
        )
        await db.commit()
    return duplicate



# ---------------- Slash-Commands ----------------
@guild_only
@bot.tree.command(name="karte", description="Ziehe eine Sammelkarte (1x alle 5h).")
async def daily_card(interaction: discord.Interaction):
    # Ephemerale (nur für dich sichtbare) "Bitte warten"-Antwort – verhindert Timeout
    await interaction.response.defer(ephemeral=True, thinking=False)

    # 1) Cooldown prüfen
    left = await get_time_left(interaction.user.id)
    if left > 0:
        hrs = left // 3600
        mins = (left % 3600) // 60
        secs = left % 60
        return await interaction.followup.send(
            f"⏳ Du kannst erst in **{hrs:02d}:{mins:02d}:{secs:02d}** wieder ziehen.",
            ephemeral=True
        )

    # 2) Karte ziehen (Rarity -> zufällige Karte dieser Seltenheit)
    
    active_set = await get_active_set(interaction.user.id)
    rarity = await pick_rarity()
    card = await pick_random_card_for_rarity(rarity, set_id=active_set)
    if card is None:
        return await interaction.followup.send(
            "⚠️ Keine Karten für diese Seltenheit gefunden. `cards.json` füllen & Bot neu starten.",
            ephemeral=True
        )

    # card enthält: (id, name, rarity, image_url, flow, punchlines, style, reputation)
    card_id, name, rarity, image_url, flow, punch, _style_ignored, _rep_ignored = card

    # 3) Inventar aktualisieren & Cooldown setzen
    duplicate = await add_to_inventory(interaction.user.id, card_id)  # True, wenn Duplikat
    await mark_pulled(interaction.user.id)

    # 4) Coins für Duplikat
    if duplicate:
        await add_coins(interaction.user.id, 5)

   # 5) Embed bauen (nur Flow & Punchlines anzeigen)
    color = get_rarity_color(rarity)
    embed = discord.Embed(
        title="🎴 Neue Karte gezogen!",
        description=f"**{name}**\nSeltenheit: **{rarity}**",
        color=color
    )
    if image_url:
        embed.set_image(url=image_url)

    stats_parts = []
    if flow is not None:
        stats_parts.append(f"Flow: **{flow}**")
    if punch is not None:
        stats_parts.append(f"Punchlines: **{punch}**")
    if stats_parts:
        embed.add_field(name="Stats", value=" · ".join(stats_parts), inline=False)

    # Footer mit Coin-Stand (+ Hinweis auf Duplikat)
    coins_now = await get_coins(interaction.user.id)
    footer = f"💰 TCG Coins: {coins_now}"
    if duplicate:
        footer = f"+5 Coins für Duplikat · {footer}"
    embed.set_footer(text=footer)

    # 6) Öffentlich im Channel posten + ephemere Bestätigung (falls Rechte fehlen, nur ephemer)
    try:
        await interaction.channel.send(
            content=f"{interaction.user.mention} hat eine Karte gezogen! 🎉",
            embed=embed
        )
        await interaction.followup.send("✅ Karte wurde im Channel gepostet.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(embed=embed, ephemeral=True)

@guild_only
@bot.tree.command(name="shop", description="TCG-Shop: 10 Coins = 1 zufällige Karte kaufen")
async def shop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id

    # Preis prüfen
    coins = await get_coins(user_id)
    if coins < 10:
        return await interaction.followup.send(f"💰 Du hast {coins} Coins. Du brauchst **10**.", ephemeral=True)

    # 10 Coins abziehen
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET coins = COALESCE(coins,0) - 10 WHERE user_id = ? AND COALESCE(coins,0) >= 10",
            (user_id,)
        )
        await db.commit()

    # Karte ziehen
    rarity = await pick_rarity()
    card = await pick_random_card_for_rarity(rarity, set_id=LATEST_SET)
    if card is None:
        return await interaction.followup.send("⚠️ Shop leer. Bitte später nochmal.", ephemeral=True)

    card_id, name, rarity, image_url, flow, punch, *_ = card

    # In Inventar packen + Duplikat prüfen
    duplicate = await add_to_inventory(user_id, card_id)

    # Duplikat → +5 Coins (per Konstante)
    coins_footer = ""
    if duplicate:
        await add_coins(user_id, DUPLICATE_COINS)  # z.B. 5
        coins_footer = f" (Duplikat: +{DUPLICATE_COINS} Coins)"

    # aktuelles Guthaben
    coins_after = await get_coins(user_id)

    # Embed bauen
    color = get_rarity_color(rarity)  # Helper: Legendary/Ultra Rare/Rare/Common → Farbe
    embed = discord.Embed(
        title="🛒 Kauf erfolgreich!",
        description=f"Du hast **{name}** gezogen (Seltenheit: **{rarity}**).",
        color=color
    )
    if image_url:
        embed.set_image(url=image_url)

    stats = []
    if flow is not None:
        stats.append(f"Flow: **{flow}**")
    if punch is not None:
        stats.append(f"Punchlines: **{punch}**")
    if stats:
        embed.add_field(name="Stats", value=" · ".join(stats), inline=False)

    embed.set_footer(text=f"💰 Coins übrig: {coins_after}{coins_footer}")

    # Posten (öffentlich, falls erlaubt) + Bestätigung an Käufer
    try:
        await interaction.channel.send(
            content=f"{interaction.user.mention} hat im Shop gekauft! 🛒",
            embed=embed
        )
        await interaction.followup.send("✅ Kauf wurde im Channel gepostet.", ephemeral=True)
    except discord.Forbidden:
        # Fallback: nur für den Nutzer anzeigen
        await interaction.followup.send(embed=embed, ephemeral=True)

class InventoryView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
        cards: list[tuple],
        start_index: int = 0,
        show_all_sets: bool = False,
        active_set: str = "S2",
        timeout: float = 120
    ):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.cards = cards
        self.index = start_index
        self.show_all_sets = show_all_sets
        self.active_set = active_set


    def build_embed(self) -> discord.Embed:
        c = self.cards[self.index]
        card_id, name, rarity, image_url, flow, punch, qty = c
        color = (
    discord.Color.purple() if rarity == "Legendary"
    else (discord.Color.gold() if rarity == "Ultra Rare"
          else (discord.Color.fuchsia() if rarity == "Rare" else discord.Color.dark_gray()))
)
        embed = discord.Embed(
            title=f"📚 Inventar – Karte {self.index+1}/{len(self.cards)}",
            description=f"**{name}** ({rarity}) · x{qty}",
            color=color
        )
        if image_url:
            embed.set_image(url=image_url)
        stats = []
        if flow is not None:  stats.append(f"Flow: **{flow}**")
        if punch is not None: stats.append(f"Punchlines: **{punch}**")
        if stats:
            embed.add_field(name="Stats", value=" · ".join(stats), inline=False)
            mode = "Alle Sets" if self.show_all_sets else f"Nur {self.active_set}"
            embed.set_footer(text=f"{mode} · Karte {self.index+1}/{len(self.cards)}")

        return embed

    async def update(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Nur der Besitzer kann hier blättern.", ephemeral=True)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="⟵ Zurück", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index - 1) % len(self.cards)
        await self.update(interaction)

    @discord.ui.button(label="Alle Sets: AUS", style=discord.ButtonStyle.primary)
    async def toggle_sets_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
            "Nur der Besitzer kann hier umschalten.",
            ephemeral=True
        )

        # Toggle umschalten
        self.show_all_sets = not self.show_all_sets
        button.label = f"Alle Sets: {'AN' if self.show_all_sets else 'AUS'}"

        # Karten neu laden
        only_set = None if self.show_all_sets else self.active_set
        self.cards = await get_inventory_full(self.user_id, only_set=only_set)

        if not self.cards:
            return await interaction.response.edit_message(
            content="📦 Keine Karten in diesem Filter.",
            embed=None,
            view=self
        )

        self.index = 0
        await interaction.response.edit_message(
        embed=self.build_embed(),
        view=self
    )

    

    @discord.ui.button(label="Weiter ⟶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = (self.index + 1) % len(self.cards)
        await self.update(interaction)

async def get_inventory_full(user_id: int, only_set: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if only_set:
            async with db.execute("""
                SELECT c.id, c.name, c.rarity, c.image_url, c.flow, c.punchlines, uc.qty
                FROM user_cards uc
                JOIN cards c ON c.id = uc.card_id
                WHERE uc.user_id = ? AND c.set_id = ?
                ORDER BY 
                    CASE c.rarity WHEN 'Legendary' THEN 4 WHEN 'Ultra Rare' THEN 3 WHEN 'Rare' THEN 2 ELSE 1 END DESC,
                    c.name ASC
            """, (user_id, only_set)) as cur:
                return await cur.fetchall()
        else:
            async with db.execute("""
                SELECT c.id, c.name, c.rarity, c.image_url, c.flow, c.punchlines, uc.qty
                FROM user_cards uc
                JOIN cards c ON c.id = uc.card_id
                WHERE uc.user_id = ?
                ORDER BY 
                    CASE c.rarity WHEN 'Legendary' THEN 4 WHEN 'Ultra Rare' THEN 3 WHEN 'Rare' THEN 2 ELSE 1 END DESC,
                    c.name ASC
            """, (user_id,)) as cur:
                return await cur.fetchall()

async def set_coins(user_id: int, value: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id, coins) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET coins = ?",
            (user_id, value, value)
        )
        await db.commit()

@guild_only
@bot.tree.command(name="inventar_suche", description="Zeigt dir eine Karte (mit Bild), falls du sie besitzt.")
@app_commands.describe(karten_id="ID oder Name der Karte", alle_sets="Wenn an: in allen Sets suchen")
@app_commands.autocomplete(karten_id=autocomplete_cards_by_name_or_id)
async def inventar_suche(interaction: discord.Interaction, karten_id: str, alle_sets: bool = False):
    await interaction.response.defer(ephemeral=True)

    # Karte finden
    card = await find_card_by_id_or_name(karten_id)
    if not card:
        return await interaction.followup.send("❌ Karte nicht gefunden.", ephemeral=True)
    cid, name, rarity, image_url = card

    # Besitz prüfen + qty holen (mit optionalem Set-Filter)
    active_set = await get_active_set(interaction.user.id)
    query = """
    SELECT uc.qty, c.flow, c.punchlines, c.set_id
    FROM user_cards uc
    JOIN cards c ON c.id = uc.card_id
    WHERE uc.user_id = ? AND uc.card_id = ?
    """
    params = [interaction.user.id, cid]

    if not alle_sets:
        query += " AND c.set_id = ?"
        params.append(active_set)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(query, tuple(params)) as cur:
            row = await cur.fetchone()

    if not row:
        scope = "in allen Sets" if alle_sets else f"in Set {active_set}"
        return await interaction.followup.send(f"📦 Du besitzt diese Karte nicht ({scope}).", ephemeral=True)

    qty, flow, punch, set_id = row

    embed = discord.Embed(
        title="🔎 Gefunden!",
        description=f"**{name}**\nSeltenheit: **{rarity}**\nSet: **{set_id}**\nMenge: **x{int(qty)}**",
        color=get_rarity_color(rarity)
    )
    if image_url:
        embed.set_image(url=image_url)

    await interaction.followup.send(embed=embed, ephemeral=True)


# /coins  → zeigt die eigenen (oder fremden) Coins
@guild_only
@bot.tree.command(name="coins", description="Zeigt deine TCG Coins (oder die eines Users).")
@app_commands.describe(user="Optional: Anderen User anzeigen")
async def coins_show(interaction: discord.Interaction, user: discord.User | None = None):
    target = user or interaction.user
    amount = await get_coins(target.id)
    # Eigene Coins ephemer anzeigen; fremde Coins auch ephemer (Datenschutz)
    if target.id == interaction.user.id:
        await interaction.response.send_message(f"💰 Du hast **{amount}** TCG Coins.", ephemeral=True)
    else:
        await interaction.response.send_message(f"💰 {target.mention} hat **{amount}** TCG Coins.", ephemeral=True)

# /coins_add → Admin gibt Coins (auch sich selbst)
@guild_only
@bot.tree.command(name="coins_add", description="(Admin) Gibt einem User TCG Coins dazu.")
@app_commands.describe(user="Wem Coins geben", amount="Anzahl der Coins (positiv)")
async def coins_add(interaction: discord.Interaction, user: discord.User, amount: int):
    # Admin-Check
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("⛔ Nur Admins dürfen Coins vergeben.", ephemeral=True)
    if amount == 0:
        return await interaction.response.send_message("Bitte eine positive Anzahl angeben.", ephemeral=True)
    if amount < 0:
        return await interaction.response.send_message("Für negative Werte nutze **/coins_set** oder rufe den Command mit positiver Zahl auf.", ephemeral=True)

    await add_coins(user.id, amount)
    new_bal = await get_coins(user.id)
    await interaction.response.send_message(
        f"✅ {user.mention} hat **+{amount}** TCG Coins erhalten. Neuer Stand: **{new_bal}**.",
        ephemeral=True
    )

# /coins_set → Admin setzt den exakten Stand (auch zum Testen nützlich)
@guild_only
@bot.tree.command(name="coins_set", description="(Admin) Setzt den exakten Coin-Stand eines Users.")
@app_commands.describe(user="Wessen Coins setzen", value="Neuer exakter Wert (>= 0)")
async def coins_set(interaction: discord.Interaction, user: discord.User, value: int):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("⛔ Nur Admins dürfen Coins setzen.", ephemeral=True)
    if value < 0:
        return await interaction.response.send_message("Wert darf nicht negativ sein.", ephemeral=True)

    await set_coins(user.id, value)
    await interaction.response.send_message(
        f"🛠️ Coins von {user.mention} auf **{value}** gesetzt.",
        ephemeral=True
    )


@guild_only
@bot.tree.command(name="inventar", description="Zeigt dein Karten-Inventar als Galerie (nur für dich sichtbar).")
async def inventory(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    # 1) aktives Set des Users holen (S1 oder S2)
    active_set = await get_active_set(interaction.user.id)

    # 2) Standardmäßig nur Karten aus dem aktiven Set laden
    cards = await get_inventory_full(interaction.user.id, only_set=active_set)

    if not cards:
        return await interaction.followup.send(
            f"📦 Du hast noch keine Karten in **{active_set}**.",
            ephemeral=True
        )

    # 3) View mit Toggle-Status initialisieren
    view = InventoryView(
        interaction.user.id,
        cards,
        start_index=0,
        show_all_sets=False,   # Toggle startet AUS
        active_set=active_set  # wichtig: View weiß, welches Set "normal" ist
    )

    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


@guild_only
@bot.tree.command(name="kartesuchen", description="Zeigt, welche User eine bestimmte Karte besitzen und wie oft.")
@app_commands.describe(karten_id="ID oder Name der Karte", öffentlich="Wenn an: im Channel posten (sonst nur für dich).")
@app_commands.autocomplete(karten_id=autocomplete_cards_by_name_or_id)
async def kartesuchen_cmd(interaction: discord.Interaction, karten_id: str, öffentlich: bool = False):
    # Standard: privat (ephemeral), öffentlich optional
    await interaction.response.defer(ephemeral=not öffentlich)

    # 1) Karte auflösen (ID oder Name)
    card = await find_card_by_id_or_name(karten_id)
    if not card:
        return await interaction.followup.send("❌ Karte nicht gefunden. Prüfe ID/Name.", ephemeral=True)
    cid, name, rarity, image_url = card

    # 2) Besitzer laden
    owners = await get_owners_for_card(cid, limit=200)

    # 3) Ausgabe vorbereiten
    def fmt_user(uid: int) -> str:
        # Erwähnung, ohne API-Call (Discord rendert <@id> → Name)
        return f"<@{uid}>"

    lines = []
    total_qty = 0
    for i, (uid, qty) in enumerate(owners, start=1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i:2d}.")
        total_qty += int(qty)
        lines.append(f"{medal} {fmt_user(uid)} — **x{int(qty)}**")

    owners_text = "\n".join(lines) if lines else "– aktuell besitzt niemand diese Karte –"

    # 4) Embed
    color = get_rarity_color(rarity) if 'get_rarity_color' in globals() else discord.Color.blurple()
    desc = f"**{name}**\nSeltenheit: **{rarity}**\nID: `{cid}`"
    embed = discord.Embed(title="🔎 Kartenbesitzer", description=desc, color=color)
    if image_url:
        embed.set_thumbnail(url=image_url)
    embed.add_field(name="Besitzer (Top)", value=owners_text, inline=False)
    embed.set_footer(text=f"Gesamt-Kopien im Umlauf: {total_qty}")

    # 5) Senden
    if öffentlich:
        try:
            await interaction.channel.send(embed=embed)
            await interaction.followup.send("✅ Im Channel gepostet.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, ephemeral=True)

@guild_only
@bot.tree.command(name="set", description="Wähle, aus welchem Set du Karten ziehst (S1 oder S2).")
@app_commands.describe(set_id="S1 oder S2")
async def set_cmd(interaction: discord.Interaction, set_id: str):
    try:
        await set_active_set(interaction.user.id, set_id)
        await interaction.response.send_message(f"✅ Dein aktives Set ist jetzt **{set_id.upper()}**.", ephemeral=True)
    except ValueError:
        await interaction.response.send_message("❌ Ungültig. Bitte **S1** oder **S2**.", ephemeral=True)



@guild_only
@bot.tree.command(name="top", description="Leaderboard: Wertvollste Sammlungen & größte Sammlungen.")
@app_commands.describe(limit="Wie viele Plätze anzeigen (Standard 10, max 25)")
async def top_leaderboard(interaction: discord.Interaction, limit: int = 10):
    limit = max(1, min(25, limit))
    await interaction.response.defer(ephemeral=False)

    # 1) Score-Board (gewichtete Punkte)
    score_rows = await get_collection_leaderboard(limit)

    # 2) Cardcount-Board (reine Menge)
    count_rows = await get_cardcount_leaderboard(limit)

    # Helper zum hübschen Anzeigen (User-Erwähnung)
    def fmt_user(uid: int) -> str:
        # Schnell & robust: Mention ohne API-Call
        return f"<@{uid}>"

    def build_table(rows, with_score: bool):
        lines = []
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, row in enumerate(rows, start=1):
            if with_score:
                uid, cards_total, score = row
                pre = medal.get(i, f"{i:2d}.")
                lines.append(f"{pre} {fmt_user(uid)} — **{int(score)} Punkte** · {int(cards_total)} Karten")
            else:
                uid, cards_total = row
                pre = medal.get(i, f"{i:2d}.")
                lines.append(f"{pre} {fmt_user(uid)} — **{int(cards_total)} Karten**")
        return "\n".join(lines) if lines else "– noch keine Daten –"

    embed = discord.Embed(
        title="🏆 Leaderboard",
        description="Ranking der **wertvollsten** Sammlungen (Score) und der **größten** Sammlungen (Menge).",
        color=discord.Color.gold()
    )
    embed.add_field(
        name="💎 Top Sammlung (Score)",
        value=build_table(score_rows, with_score=True),
        inline=False
    )
    embed.add_field(
        name="📦 Top Kartenanzahl",
        value=build_table(count_rows, with_score=False),
        inline=False
    )
    embed.set_footer(text="Punkte: Common=1, Rare=2, Ultra Rare=5, Legendary=10")

    await interaction.followup.send(embed=embed)




if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ Kein DISCORD_TOKEN in .env gefunden!")
    bot.run(TOKEN)
