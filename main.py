from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import requests
import base64
import os
from datetime import datetime

app = FastAPI()

# Allow your Flet Web app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# CONFIG
# ----------------------------
OWNER = "mechagdynia2-ai"
REPO = "game_multiplayer"
FILE_PATH = "assets/leaderboard.json"
API_URL = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{FILE_PATH}"

TOKEN = os.getenv("GITHUB_TOKEN")
if not TOKEN:
    print("⚠ WARNING: Missing GITHUB_TOKEN environment variable")


# ----------------------------
# READ leaderboard.json FROM GITHUB
# ----------------------------
def get_leaderboard():
    headers = {"Authorization": f"token {TOKEN}"}
    r = requests.get(API_URL, headers=headers)

    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"GitHub GET error: {r.text}")

    data = r.json()
    content = base64.b64decode(data["content"]).decode()
    sha = data["sha"]

    import json
    return json.loads(content), sha


# ----------------------------
# WRITE leaderboard.json TO GITHUB
# ----------------------------
def update_leaderboard(new_data, sha):
    headers = {"Authorization": f"token {TOKEN}"}
    import json

    encoded = base64.b64encode(json.dumps(new_data, indent=2).encode()).decode()

    payload = {
        "message": "Update leaderboard",
        "content": encoded,
        "sha": sha,
    }

    r = requests.put(API_URL, json=payload, headers=headers)
    if r.status_code not in [200, 201]:
        raise HTTPException(status_code=500, detail=f"GitHub PUT error: {r.text}")


# ----------------------------
# ROUTES
# ----------------------------

@app.get("/")
def index():
    return {"message": "Awantura o Kasę Multiplayer – Backend działa 🎉"}


@app.get("/leaderboard")
def leaderboard():
    data, _ = get_leaderboard()
    return data


@app.post("/submit")
def submit_score(player: str, score: int, time: int = 0):
    lb, sha = get_leaderboard()

    new_entry = {
        "player": player,
        "score": score,
        "time": time,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M")
    }

    lb.append(new_entry)

    # Sort by score DESC
    lb = sorted(lb, key=lambda x: x["score"], reverse=True)

    update_leaderboard(lb, sha)

    return {"status": "OK", "saved": new_entry}
