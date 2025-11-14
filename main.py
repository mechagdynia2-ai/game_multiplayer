from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import json
import time
from pathlib import Path

app = FastAPI()

DATA_DIR = Path("./data")
DATA_DIR.mkdir(exist_ok=True)

PLAYERS_FILE = DATA_DIR / "players.json"
CHAT_FILE = DATA_DIR / "chat.json"
LEADERBOARD_FILE = DATA_DIR / "leaderboard.json"

for file in [PLAYERS_FILE, CHAT_FILE, LEADERBOARD_FILE]:
    if not file.exists():
        file.write_text("[]", encoding="utf-8")

def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except:
        return []

def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"message": "Awantura o Kasę Multiplayer – Backend działa 🎉"}

@app.get("/leaderboard")
def get_leaderboard():
    return load_json(LEADERBOARD_FILE)

@app.post("/submit")
def submit_score(data: dict):
    leaderboard = load_json(LEADERBOARD_FILE)

    entry = {
        "player": data.get("player", "Anon"),
        "score": data.get("score", 0),
        "time": data.get("time", 0),
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    leaderboard.append(entry)
    leaderboard = sorted(leaderboard, key=lambda x: x["score"], reverse=True)

    save_json(LEADERBOARD_FILE, leaderboard)
    return {"status": "OK", "saved": entry}

@app.get("/players")
def get_players():
    players = load_json(PLAYERS_FILE)
    now = time.time()
    players = [p for p in players if now - p["last_seen"] < 60]
    save_json(PLAYERS_FILE, players)
    return players

@app.post("/players/join")
def player_join(data: dict):
    name = data.get("name", "").strip()
    if not name:
        return {"error": "No name"}

    players = load_json(PLAYERS_FILE)
    now = time.time()

    for p in players:
        if p["name"] == name:
            p["last_seen"] = now
            save_json(PLAYERS_FILE, players)
            return {"status": "updated", "name": name}

    players.append({"name": name, "last_seen": now})
    save_json(PLAYERS_FILE, players)
    return {"status": "joined", "name": name}

@app.get("/chat")
def get_chat():
    return load_json(CHAT_FILE)[-50:]

@app.post("/chat/send")
def send_message(data: dict):
    name = data.get("name", "Anon")
    msg = data.get("msg", "").strip()
    if not msg:
        return {"error": "empty message"}

    chat = load_json(CHAT_FILE)
    chat.append({
        "name": name,
        "msg": msg,
        "time": time.strftime("%H:%M:%S")
    })

    save_json(CHAT_FILE, chat)
    return {"status": "sent"}
