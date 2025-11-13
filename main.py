import os
import base64
import json
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ⬇ ważne: to jest TWOJE repo z grą multiplayer
GITHUB_REPO = os.getenv("GITHUB_REPO", "mechagdynia2-ai/game_multiplayer")

# ⬇ ścieżka do pliku z rankingiem w repo
# jeśli Twój plik ma inną nazwę niż leaderboard.json,
# zmień to na np. "assets/ranking.json"
GITHUB_FILE_PATH = os.getenv("GITHUB_FILE_PATH", "assets/leaderboard.json")

GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

if not GITHUB_TOKEN:
    raise RuntimeError("Brak zmiennej środowiskowej GITHUB_TOKEN")


app = FastAPI()

# CORS – pozwalamy na wywołania z Twojej gry w przeglądarce
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # później można zawęzić
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Score(BaseModel):
    player: str
    score: int
    time: int  # czas w sekundach


async def _get_file():
    """
    Pobiera leaderboard.json z GitHuba (API contents)
    i zwraca (lista_wyników, sha_pliku).
    """
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers)

    if r.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=f"GitHub GET error ({r.status_code}): {r.text}",
        )

    data = r.json()
    content_raw = base64.b64decode(data["content"]).decode("utf-8")

    try:
        leaderboard = json.loads(content_raw)
        if not isinstance(leaderboard, list):
            leaderboard = []
    except Exception:
        leaderboard = []

    return leaderboard, data["sha"]


async def _put_file(leaderboard, sha: str):
    """
    Zapisuje zaktualizowany leaderboard z powrotem do GitHuba.
    """
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    new_content = json.dumps(leaderboard, ensure_ascii=False, indent=2)
    b64_content = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")

    payload = {
        "message": "Update leaderboard from game",
        "content": b64_content,
        "sha": sha,
        "branch": GITHUB_BRANCH,
    }

    async with httpx.AsyncClient() as client:
        r = await client.put(url, headers=headers, json=payload)

    if r.status_code not in (200, 201):
        raise HTTPException(
            status_code=500,
            detail=f"GitHub PUT error ({r.status_code}): {r.text}",
        )


@app.get("/leaderboard")
async def get_leaderboard():
    """
    Zwraca posortowany ranking (top 50).
    """
    leaderboard, _ = await _get_file()

    # sortujemy: najpierw po score malejąco, potem po czasie rosnąco
    leaderboard_sorted = sorted(
        leaderboard,
        key=lambda item: (-int(item.get("score", 0)), int(item.get("time", 0))),
    )
    return leaderboard_sorted[:50]


@app.post("/submit")
async def submit(score: Score):
    """
    Dodaje nowy wpis do rankingu.
    """
    leaderboard, sha = await _get_file()

    entry = {
        "player": score.player[:32],  # max 32 znaki
        "score": int(score.score),
        "time": int(score.time),
        "date": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    }

    leaderboard.append(entry)

    # żeby plik nie rósł w nieskończoność – trzymamy np. ostatnie 200 wyników
    if len(leaderboard) > 200:
        leaderboard = leaderboard[-200:]

    await _put_file(leaderboard, sha)

    return {"status": "ok", "saved": entry}
