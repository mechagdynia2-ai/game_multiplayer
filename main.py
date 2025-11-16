from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import time
import uuid

app = FastAPI(title="Awantura o Kasę – Multiplayer Backend")

origins = [
    "https://mechagdynia2-ai.github.io",
    "https://mechagdynia2-ai.github.io/awantura_o_kase_multiplayer",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------
#    MODELE
# ------------------------------------------------------------

class Player(BaseModel):
    id: str
    name: str
    money: int = 10000
    is_admin: bool = False
    is_observer: bool = False    # Dodane 
    last_seen: float = 0.0

class PlayerState(BaseModel):
    id: str
    name: str
    money: int
    bid: int
    is_all_in: bool
    is_admin: bool
    is_observer: bool

class BidInfo(BaseModel):
    player_id: str
    amount: int
    is_all_in: bool
    ts: float

class ChatMessage(BaseModel):
    player: str
    message: str
    timestamp: float

class RegisterRequest(BaseModel):
    name: str

class BidRequest(BaseModel):
    player_id: str
    kind: str  

class ChatRequest(BaseModel):
    player: str
    message: str

class HeartbeatRequest(BaseModel):
    player_id: str

class FinishBiddingRequest(BaseModel):
    player_id: str   # kto zakończył licytację

class AdminKickRequest(BaseModel):
    admin_id: str
    target_id: str

class GameStatusResponse(BaseModel):
    game_over: bool
    winner: Optional[str] = None
    reason: Optional[str] = None

class StateResponse(BaseModel):
    round_id: int
    phase: str
    pot: int
    time_left: float
    answering_player_id: Optional[str]
    players: List[PlayerState]
    chat: List[ChatMessage]

# ------------------------------------------------------------
#    STAN SERWERA
# ------------------------------------------------------------

PLAYERS: Dict[str, Player] = {}
BIDS: Dict[str, BidInfo] = {}
CHAT: List[ChatMessage] = []

ROUND_ID = 1
PHASE = "bidding"
ROUND_START_TS = time.time()
BIDDING_DURATION = 20.0
POT = 0
ANSWERING_PLAYER_ID: Optional[str] = None
AFK_TIMEOUT = 30.0
GAME_OVER = False
WINNER: Optional[str] = None

# ------------------------------------------------------------
#    POMOCNICZE
# ------------------------------------------------------------

def _touch(player_id):
    p = PLAYERS.get(player_id)
    if p:
        p.last_seen = time.time()

def _cleanup_afk():
    global ANSWERING_PLAYER_ID, POT

    now = time.time()
    to_remove = []

    for pid, p in PLAYERS.items():
        if now - p.last_seen > AFK_TIMEOUT:
            to_remove.append(pid)

    for pid in to_remove:
        name = PLAYERS[pid].name
        del PLAYERS[pid]
        if pid in BIDS:
            del BIDS[pid]
        if pid == ANSWERING_PLAYER_ID:
            ANSWERING_PLAYER_ID = None
        CHAT.append(ChatMessage(player="BOT", message=f"{name} został usunięty (AFK).", timestamp=time.time()))

    _recompute_pot()

def _recompute_pot():
    global POT
    POT = sum(b.amount for b in BIDS.values())

def _time_left():
    if PHASE != "bidding":
        return 0.0
    now = time.time()
    left = BIDDING_DURATION - (now - ROUND_START_TS)
    return max(0, left)

def _auto_finish_if_needed():
    if PHASE == "bidding" and _time_left() <= 0:
        _finish_bidding(trigger="timer")

def _finish_bidding(trigger="manual"):
    """
    - wybór zwycięzcy
    - przejście do fazy answering
    """
    global PHASE, ANSWERING_PLAYER_ID

    if not BIDS:
        ANSWERING_PLAYER_ID = None
        PHASE = "answering"
        return

    best = None
    for b in BIDS.values():
        if best is None:
            best = b
        else:
            if b.amount > best.amount or (b.amount == best.amount and b.ts < best.ts):
                best = b

    ANSWERING_PLAYER_ID = best.player_id
    PHASE = "answering"

def _start_new_round():
    global ROUND_ID, PHASE, ROUND_START_TS, POT, ANSWERING_PLAYER_ID, BIDS
    ROUND_ID += 1
    PHASE = "bidding"
    ROUND_START_TS = time.time()
    ANSWERING_PLAYER_ID = None
    BIDS = {}
    POT = 0

def _check_endgame_conditions():
    """
    Zasady końca gry (pkt. 10–11):
    """
    global GAME_OVER, WINNER

    active = [p for p in PLAYERS.values() if not p.is_observer]

    if len(active) == 0:
        return

    # 10 — jeśli gracz ma <500 → zostaje obserwatorem
    for p in active:
        if p.money < 500:
            p.is_observer = True

    active = [p for p in PLAYERS.values() if not p.is_observer]

    # 11b — tylko jeden gracz ma >=500 → natychmiast wygrywa
    if len(active) == 1:
        winner = active[0]
        # przejmuje wszystko
        for p in PLAYERS.values():
            if p.id != winner.id:
                winner.money += p.money
                p.money = 0
        winner.money += POT
        POT = 0
        GAME_OVER = True
        WINNER = winner.name
        return

    # 11a — gra skończyła się naturalnie — ostatnie pytanie
    # → zwycięzca = najwięcej pieniędzy
    # (frontend musi wysłać informację "last question answered")
    return
# ============================================================
#   ENDPOINTY
# ============================================================

@app.get("/")
def root():
    return {"message": "Backend działa 🎉", "docs": "/docs"}


# ------------------------------------------------------------
#   REJESTRACJA GRACZA
# ------------------------------------------------------------

@app.post("/register", response_model=Player)
def register_player(req: RegisterRequest):
    player_id = str(uuid.uuid4())
    is_first = len(PLAYERS) == 0

    p = Player(
        id=player_id,
        name=req.name,
        money=10000,
        is_admin=is_first,
        is_observer=False,
        last_seen=time.time(),
    )
    PLAYERS[player_id] = p

    CHAT.append(ChatMessage(
        player="BOT",
        message=f"{req.name} dołączył do gry.",
        timestamp=time.time(),
    ))

    return p


# ------------------------------------------------------------
#   HEARTBEAT
# ------------------------------------------------------------

@app.post("/heartbeat")
def heartbeat(req: HeartbeatRequest):
    _cleanup_afk()

    if req.player_id not in PLAYERS:
        raise HTTPException(404, "Gracz zniknął lub został usunięty.")

    PLAYERS[req.player_id].last_seen = time.time()
    return {"status": "ok", "is_admin": PLAYERS[req.player_id].is_admin}


# ------------------------------------------------------------
#   CZAT
# ------------------------------------------------------------

@app.post("/chat")
def post_chat(req: ChatRequest):
    _cleanup_afk()

    CHAT.append(ChatMessage(
        player=req.player,
        message=req.message,
        timestamp=time.time(),
    ))

    if len(CHAT) > 200:
        del CHAT[:-200]

    return {"status": "ok"}


@app.get("/chat", response_model=List[ChatMessage])
def get_chat():
    _cleanup_afk()
    return CHAT[-50:]


# ------------------------------------------------------------
#   LICYTACJA
# ------------------------------------------------------------

@app.post("/bid")
def bid(req: BidRequest):
    global PHASE

    if GAME_OVER:
        raise HTTPException(400, "Gra już się zakończyła.")

    if req.player_id not in PLAYERS:
        raise HTTPException(404, "Nieznany gracz.")

    _cleanup_afk()
    _auto_finish_if_needed()

    p = PLAYERS[req.player_id]

    if p.is_observer:
        raise HTTPException(400, "Jesteś obserwatorem — nie możesz licytować.")

    if PHASE != "bidding":
        raise HTTPException(400, "Licytacja jest już zamknięta.")

    now = time.time()

    if req.kind == "normal":
        if p.money < 100:
            raise HTTPException(400, "Nie masz 100 zł na postawienie.")
        p.money -= 100

        if req.player_id not in BIDS:
            BIDS[req.player_id] = BidInfo(
                player_id=req.player_id,
                amount=100,
                is_all_in=False,
                ts=now
            )
        else:
            BIDS[req.player_id].amount += 100
            BIDS[req.player_id].ts = now

        _recompute_pot()

        CHAT.append(ChatMessage(
            player=p.name,
            message=f"licytuje +100 zł (łącznie {BIDS[req.player_id].amount} zł)",
            timestamp=time.time(),
        ))

        return {"status": "ok", "pot": POT}

    elif req.kind == "allin":
        if p.money <= 0:
            raise HTTPException(400, "Nie możesz iść va banque z 0 zł.")

        add = p.money
        p.money = 0

        if req.player_id not in BIDS:
            BIDS[req.player_id] = BidInfo(
                player_id=req.player_id,
                amount=add,
                is_all_in=True,
                ts=now
            )
        else:
            BIDS[req.player_id].amount += add
            BIDS[req.player_id].is_all_in = True
            BIDS[req.player_id].ts = now

        _recompute_pot()
        _finish_bidding(trigger="allin")

        CHAT.append(ChatMessage(
            player=p.name,
            message=f"poszedł VA BANQUE ({BIDS[req.player_id].amount} zł)!",
            timestamp=time.time(),
        ))

        return {"status": "ok", "pot": POT, "phase": PHASE}

    else:
        raise HTTPException(400, "Nieznany rodzaj licytacji.")


# ------------------------------------------------------------
#   RĘCZNE ZAKOŃCZENIE LICYTACJI (funkcja 9b)
# ------------------------------------------------------------

@app.post("/finish_bidding")
def finish_bidding(req: FinishBiddingRequest):
    global PHASE

    if GAME_OVER:
        raise HTTPException(400, "Gra już jest zakończona.")

    if req.player_id not in PLAYERS:
        raise HTTPException(404, "Nieznany gracz.")

    p = PLAYERS[req.player_id]

    if p.is_observer:
        raise HTTPException(400, "Obserwator nie może zakończyć licytacji.")

    _finish_bidding(trigger="manual")

    CHAT.append(ChatMessage(
        player=p.name,
        message="kończy licytację!",
        timestamp=time.time(),
    ))

    return {"status": "ok", "phase": PHASE}


# ------------------------------------------------------------
#   NOWA RUNDA – ADMIN
# ------------------------------------------------------------

@app.post("/next_round")
def next_round():
    global GAME_OVER

    if GAME_OVER:
        raise HTTPException(400, "Gra już się zakończyła.")

    _cleanup_afk()
    _start_new_round()

    CHAT.append(ChatMessage(
        player="BOT",
        message="Rozpoczyna się nowa runda.",
        timestamp=time.time(),
    ))

    return {"status": "ok", "round_id": ROUND_ID}


# ------------------------------------------------------------
#   ADMIN → WYRZUCENIE GRACZA
# ------------------------------------------------------------

@app.post("/admin/kick")
def admin_kick(req: AdminKickRequest):
    if req.admin_id not in PLAYERS or not PLAYERS[req.admin_id].is_admin:
        raise HTTPException(403, "Brak uprawnień admina.")

    if req.target_id not in PLAYERS:
        raise HTTPException(404, "Taki gracz nie istnieje.")

    name = PLAYERS[req.target_id].name
    del PLAYERS[req.target_id]
    if req.target_id in BIDS:
        del BIDS[req.target_id]

    CHAT.append(ChatMessage(
        player="BOT",
        message=f"{name} został wyrzucony przez ADMINA.",
        timestamp=time.time(),
    ))

    return {"status": "ok"}


# ------------------------------------------------------------
#   STAN GRY
# ------------------------------------------------------------

@app.get("/state", response_model=StateResponse)
def state():
    _cleanup_afk()
    _auto_finish_if_needed()

    players_state = []
    for pid, p in PLAYERS.items():
        bid_amount = BIDS[pid].amount if pid in BIDS else 0
        players_state.append(PlayerState(
            id=p.id,
            name=p.name,
            money=p.money,
            bid=bid_amount,
            is_all_in=BIDS[pid].is_all_in if pid in BIDS else False,
            is_admin=p.is_admin,
            is_observer=p.is_observer,
        ))

    chat = CHAT[-30:]

    return StateResponse(
        round_id=ROUND_ID,
        phase=PHASE,
        pot=POT,
        time_left=_time_left(),
        answering_player_id=ANSWERING_PLAYER_ID,
        players=players_state,
        chat=chat,
    )


# ------------------------------------------------------------
#   STATUS GRY (czy już koniec)
# ------------------------------------------------------------

@app.get("/game_status", response_model=GameStatusResponse)
def game_status():
    if GAME_OVER:
        return GameStatusResponse(
            game_over=True,
            winner=WINNER,
            reason="Brak aktywnych graczy lub zakończono ostatnie pytanie."
        )
    return GameStatusResponse(game_over=False)
