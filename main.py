from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import time
import uuid

app = FastAPI(title="Awantura o Kasę – Multiplayer Backend")

# --- CORS -------------------------------------------------------------------

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

# --- MODELE DANYCH ----------------------------------------------------------


class Player(BaseModel):
    id: str
    name: str
    money: int = 10000
    created_ts: float  # kiedy dołączył
    last_seen: float   # ostatnia aktywność (bid/heartbeat/chat)
    is_admin: bool = False


class PlayerState(BaseModel):
    id: str
    name: str
    money: int
    bid: int
    is_all_in: bool
    is_admin: bool  # informacja dla frontendu


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
    kind: str  # "normal" albo "allin"


class ChatRequest(BaseModel):
    player: str
    message: str


class SubmitScore(BaseModel):
    player: str
    score: int
    time: int


class LeaderboardEntry(BaseModel):
    player: str
    score: int
    time: int
    date: float


class HeartbeatRequest(BaseModel):
    player_id: str


class StateResponse(BaseModel):
    round_id: int
    phase: str
    pot: int
    time_left: float
    answering_player_id: Optional[str]
    players: List[PlayerState]
    chat: List[ChatMessage]


# --- STAN SERWERA (IN-MEMORY) ----------------------------------------------

PLAYERS: Dict[str, Player] = {}
BIDS: Dict[str, BidInfo] = {}
CHAT: List[ChatMessage] = []
LEADERBOARD: List[LeaderboardEntry] = []

ROUND_ID: int = 1
PHASE: str = "bidding"  # "bidding" | "answering"
ROUND_START_TS: float = time.time()
BIDDING_DURATION: float = 20.0
POT: int = 0
ANSWERING_PLAYER_ID: Optional[str] = None

ADMIN_ID: Optional[str] = None        # aktualny admin
INACTIVITY_TIMEOUT: float = 30.0      # po tylu sekundach gracz jest usuwany

# --- FUNKCJE POMOCNICZE -----------------------------------------------------


def _recompute_pot() -> None:
    """Przelicz sumę puli na podstawie BIDS."""
    global POT
    POT = sum(b.amount for b in BIDS.values())


def _time_left() -> float:
    """Ile sekund zostało do końca licytacji."""
    if PHASE != "bidding":
        return 0.0
    now = time.time()
    left = BIDDING_DURATION - (now - ROUND_START_TS)
    return max(0.0, left)


def _auto_finish_if_needed() -> None:
    """Jeśli czas licytacji minął – kończymy licytację."""
    global PHASE
    if PHASE == "bidding" and _time_left() <= 0:
        _finish_bidding(trigger="timer")


def _finish_bidding(trigger: str) -> None:
    """
    Wybór gracza odpowiadającego po zakończeniu licytacji.
    trigger: "timer" albo "allin"
    """
    global PHASE, ANSWERING_PLAYER_ID

    if not BIDS:
        ANSWERING_PLAYER_ID = None
        PHASE = "answering"
        return

    best: Optional[BidInfo] = None
    for bid in BIDS.values():
        if best is None:
            best = bid
        else:
            if bid.amount > best.amount:
                best = bid
            elif bid.amount == best.amount and bid.ts < best.ts:
                best = bid

    ANSWERING_PLAYER_ID = best.player_id if best else None
    PHASE = "answering"


def _start_new_round() -> None:
    """Reset stanu rundy i przejście do kolejnej licytacji."""
    global ROUND_ID, PHASE, ROUND_START_TS, POT, ANSWERING_PLAYER_ID, BIDS
    ROUND_ID += 1
    PHASE = "bidding"
    ROUND_START_TS = time.time()
    POT = 0
    ANSWERING_PLAYER_ID = None
    BIDS = {}


def _assign_admin_if_needed() -> None:
    """Jeśli nie ma admina, wyznacz go (najstarszy gracz)."""
    global ADMIN_ID
    if ADMIN_ID is not None and ADMIN_ID in PLAYERS:
        return
    if not PLAYERS:
        ADMIN_ID = None
        return

    # najstarszy (najmniejszy created_ts)
    oldest = min(PLAYERS.values(), key=lambda p: p.created_ts)
    ADMIN_ID = oldest.id
    for p in PLAYERS.values():
        p.is_admin = (p.id == ADMIN_ID)


def _drop_inactive_players() -> None:
    """
    Usuwa graczy nieaktywnych dłużej niż INACTIVITY_TIMEOUT
    (nie ma heartbeat, bidów ani wysyłania wiadomości).
    """
    global ADMIN_ID, ANSWERING_PLAYER_ID

    now = time.time()
    to_delete = [
        pid
        for pid, p in PLAYERS.items()
        if (now - p.last_seen) > INACTIVITY_TIMEOUT
    ]

    if not to_delete:
        return

    for pid in to_delete:
        del PLAYERS[pid]
        if pid in BIDS:
            del BIDS[pid]
        if pid == ANSWERING_PLAYER_ID:
            ANSWERING_PLAYER_ID = None

    # po usunięciu – przepisz admina, jeśli trzeba
    _assign_admin_if_needed()
    _recompute_pot()


# --- ENDPOINTY --------------------------------------------------------------


@app.get("/")
def root():
    return {
        "message": "Awantura o Kasę Multiplayer – Backend działa 🎉",
        "docs": "/docs",
    }


@app.post("/register", response_model=Player)
def register_player(req: RegisterRequest) -> Player:
    """
    Rejestracja nowego gracza.
    Pierwszy gracz zostaje ADMINEM.
    """
    global ADMIN_ID

    now = time.time()
    player_id = str(uuid.uuid4())

    is_first_player = len(PLAYERS) == 0
    is_admin = is_first_player

    player = Player(
        id=player_id,
        name=req.name,
        money=10000,
        created_ts=now,
        last_seen=now,
        is_admin=is_admin,
    )
    PLAYERS[player_id] = player

    if is_admin:
        ADMIN_ID = player_id
    else:
        _assign_admin_if_needed()

    return player


@app.get("/players", response_model=List[Player])
def list_players():
    """Surowa lista graczy (do debug / admin)."""
    _drop_inactive_players()
    return list(PLAYERS.values())


@app.post("/heartbeat")
def heartbeat(req: HeartbeatRequest):
    """
    Gracz wysyła heartbeat, żeby nie zostać wyrzuconym po 30s.
    Można wywoływać np. co 10 sekund z frontendu.
    """
    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nie ma takiego gracza.")

    now = time.time()
    PLAYERS[req.player_id].last_seen = now
    _drop_inactive_players()
    _assign_admin_if_needed()

    return {
        "status": "ok",
        "is_admin": PLAYERS[req.player_id].is_admin,
    }


@app.post("/bid")
def place_bid(req: BidRequest):
    global PHASE

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nie ma takiego gracza.")

    # aktualizacja aktywności
    PLAYERS[req.player_id].last_seen = time.time()
    _drop_inactive_players()

    _auto_finish_if_needed()

    if PHASE != "bidding":
        raise HTTPException(
            status_code=400,
            detail="Ta runda nie jest już w fazie licytacji.",
        )

    player = PLAYERS[req.player_id]
    now = time.time()

    if req.kind == "normal":
        cost = 100
        if player.money < cost:
            raise HTTPException(
                status_code=400,
                detail="Za mało kasy na licytację +100.",
            )
        player.money -= cost

        if req.player_id in BIDS:
            b = BIDS[req.player_id]
            b.amount += cost
            b.ts = now
        else:
            BIDS[req.player_id] = BidInfo(
                player_id=req.player_id,
                amount=cost,
                is_all_in=False,
                ts=now,
            )

        _recompute_pot()
        return {"status": "ok", "pot": POT}

    elif req.kind == "allin":
        if player.money <= 0:
            raise HTTPException(
                status_code=400,
                detail="Nie możesz iść va banque z 0 zł.",
            )

        add_amount = player.money
        player.money = 0

        if req.player_id in BIDS:
            b = BIDS[req.player_id]
            b.amount += add_amount
            b.is_all_in = True
            b.ts = now
        else:
            BIDS[req.player_id] = BidInfo(
                player_id=req.player_id,
                amount=add_amount,
                is_all_in=True,
                ts=now,
            )

        _recompute_pot()
        _finish_bidding(trigger="allin")
        return {"status": "ok", "pot": POT, "phase": PHASE}

    else:
        raise HTTPException(status_code=400, detail="Nieznany rodzaj licytacji.")


@app.post("/next_round")
def next_round():
    """
    Przejście do kolejnej rundy.
    (Na razie bez sprawdzania admina – można dodać w przyszłości.)
    """
    _start_new_round()
    return {"status": "ok", "round_id": ROUND_ID}


@app.get("/state", response_model=StateResponse)
def get_state():
    """
    Aktualny stan gry: runda, faza, pula, timer, gracze, chat.
    """
    _drop_inactive_players()
    _auto_finish_if_needed()

    players_state: List[PlayerState] = []

    for pid, p in PLAYERS.items():
        bid_info = BIDS.get(pid)
        bid_amount = bid_info.amount if bid_info else 0
        is_all_in = bid_info.is_all_in if bid_info else False
        players_state.append(
            PlayerState(
                id=p.id,
                name=p.name,
                money=p.money,
                bid=bid_amount,
                is_all_in=is_all_in,
                is_admin=p.is_admin,
            )
        )

    chat_slice = CHAT[-30:]

    return StateResponse(
        round_id=ROUND_ID,
        phase=PHASE,
        pot=POT,
        time_left=_time_left(),
        answering_player_id=ANSWERING_PLAYER_ID,
        players=players_state,
        chat=chat_slice,
    )


@app.post("/chat")
def post_chat(msg: ChatRequest):
    """
    Prosty czat – NIE jest powiązany ściśle z player_id,
    ale wiadomość liczy się jako aktywność gracza, jeśli istnieje.
    """
    now = time.time()
    # jeśli ktoś ma taki nick – potraktuj jako aktywność
    for p in PLAYERS.values():
        if p.name == msg.player:
            p.last_seen = now
            break

    CHAT.append(
        ChatMessage(
            player=msg.player,
            message=msg.message,
            timestamp=now,
        )
    )
    if len(CHAT) > 200:
        del CHAT[:-200]
    _drop_inactive_players()
    return {"status": "ok"}


@app.get("/chat", response_model=List[ChatMessage])
def get_chat():
    _drop_inactive_players()
    return CHAT[-50:]


@app.post("/submit")
def submit_score(score: SubmitScore):
    entry = LeaderboardEntry(
        player=score.player,
        score=score.score,
        time=score.time,
        date=time.time(),
    )
    LEADERBOARD.append(entry)
    LEADERBOARD.sort(key=lambda e: e.score, reverse=True)
    if len(LEADERBOARD) > 100:
        del LEADERBOARD[100:]
    return {"status": "ok"}


@app.get("/leaderboard")
def get_leaderboard():
    return LEADERBOARD[:50]
