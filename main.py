from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import time
import uuid

app = FastAPI(title="Awantura o Kasę – Multiplayer Backend")

# --- CORS -----------------------------------------------------------------

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


# --- MODELE DANYCH --------------------------------------------------------


class Player(BaseModel):
    id: str
    name: str
    money: int = 10000
    is_admin: bool = False
    last_seen: float = 0.0  # timestamp ostatniego heartbeat/state


class PlayerState(BaseModel):
    id: str
    name: str
    money: int
    bid: int
    is_all_in: bool
    is_admin: bool


class BidInfo(BaseModel):
    player_id: str
    amount: int
    is_all_in: bool
    ts: float  # kiedy złożono ostatnią ofertę


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


class HeartbeatRequest(BaseModel):
    player_id: str


class SubmitScore(BaseModel):
    player: str
    score: int
    time: int


class LeaderboardEntry(BaseModel):
    player: str
    score: int
    time: int
    date: float


class StateResponse(BaseModel):
    round_id: int
    phase: str
    pot: int
    time_left: float
    answering_player_id: Optional[str]
    players: List[PlayerState]
    chat: List[ChatMessage]


# --- STAN SERWERA ---------------------------------------------------------


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

AFK_TIMEOUT: float = 30.0  # sekundy bez heartbeat/state → wyrzucenie


# --- FUNKCJE POMOCNICZE ---------------------------------------------------


def _recompute_pot() -> None:
    global POT
    POT = sum(b.amount for b in BIDS.values())


def _time_left() -> float:
    if PHASE != "bidding":
        return 0.0
    now = time.time()
    left = BIDDING_DURATION - (now - ROUND_START_TS)
    return max(0.0, left)


def _auto_finish_if_needed() -> None:
    """Jeśli czas licytacji minął — kończymy ją."""
    global PHASE
    if PHASE == "bidding" and _time_left() <= 0:
        _finish_bidding(trigger="timer")


def _finish_bidding(trigger: str) -> None:
    """
    Wybór zwycięzcy licytacji:
    - największa kwota
    - przy remisie: wcześniejszy timestamp
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

    if best is not None:
        ANSWERING_PLAYER_ID = best.player_id
    else:
        ANSWERING_PLAYER_ID = None

    PHASE = "answering"


def _start_new_round() -> None:
    """Ręczne rozpoczęcie nowej rundy (admin, /next_round)."""
    global ROUND_ID, PHASE, ROUND_START_TS, POT, ANSWERING_PLAYER_ID, BIDS
    ROUND_ID += 1
    PHASE = "bidding"
    ROUND_START_TS = time.time()
    POT = 0
    ANSWERING_PLAYER_ID = None
    BIDS = {}


def _cleanup_afk() -> None:
    """
    Usuwa graczy, którzy nie wysłali heartbeat /state od AFK_TIMEOUT sekund.
    Czyści też ich stawki i przelicza pulę.
    """
    global ANSWERING_PLAYER_ID

    now = time.time()
    to_delete = []

    for pid, player in PLAYERS.items():
        if now - player.last_seen > AFK_TIMEOUT:
            to_delete.append(pid)

    if not to_delete:
        return

    for pid in to_delete:
        print(f"[AFK] Usuwam gracza {pid} ({PLAYERS[pid].name})")
        del PLAYERS[pid]
        if pid in BIDS:
            del BIDS[pid]
        if pid == ANSWERING_PLAYER_ID:
            ANSWERING_PLAYER_ID = None

    _recompute_pot()


def _touch_player(player_id: str) -> None:
    """Aktualizacja last_seen (przy heartbeat i state, gdy znamy id)."""
    p = PLAYERS.get(player_id)
    if p:
        p.last_seen = time.time()


# --- ENDPOINTY ------------------------------------------------------------


@app.get("/")
def root():
    return {
        "message": "Awantura o Kasę Multiplayer – Backend działa 🎉",
        "docs": "/docs",
    }


# --- REJESTRACJA GRACZA ---------------------------------------------------


@app.post("/register", response_model=Player)
def register_player(req: RegisterRequest):
    """
    Rejestruje gracza; pierwszy gracz w systemie zostaje ADMINEM.
    """
    player_id = str(uuid.uuid4())
    is_first_player = len(PLAYERS) == 0

    player = Player(
        id=player_id,
        name=req.name,
        money=10000,
        is_admin=is_first_player,
        last_seen=time.time(),
    )
    PLAYERS[player_id] = player
    print(f"[REGISTER] {req.name} ({player_id}), admin={is_first_player}")
    return player


@app.get("/players", response_model=List[Player])
def list_players():
    _cleanup_afk()
    return list(PLAYERS.values())


# --- HEARTBEAT (utrzymanie przy życiu) -----------------------------------


@app.post("/heartbeat")
def heartbeat(req: HeartbeatRequest):
    """
    Frontend wywołuje co ~10 s.

    Aktualizujemy last_seen i zwracamy info, czy gracz jest adminem.
    Jeśli gracza nie ma (bo wygasł / został wyrzucony) → 404.
    """
    _cleanup_afk()

    player = PLAYERS.get(req.player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Gracz nie istnieje (AFK lub nieznany).")

    player.last_seen = time.time()
    return {"status": "ok", "is_admin": player.is_admin}


# --- LICYTACJA ------------------------------------------------------------


@app.post("/bid")
def place_bid(req: BidRequest):
    global PHASE

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nie ma takiego gracza.")

    _cleanup_afk()
    _auto_finish_if_needed()

    if PHASE != "bidding":
        raise HTTPException(
            status_code=400,
            detail="Ta runda nie jest już w fazie licytacji.",
        )

    player = PLAYERS[req.player_id]
    player.last_seen = time.time()
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
        raise HTTPException(
            status_code=400,
            detail="Nieznany rodzaj licytacji.",
        )


# --- NOWA RUNDA (np. wywoływana przez admina z frontu) --------------------


@app.post("/next_round")
def next_round():
    _cleanup_afk()
    _start_new_round()
    return {"status": "ok", "round_id": ROUND_ID}


# --- STAN GRY -------------------------------------------------------------


@app.get("/state", response_model=StateResponse)
def get_state():
    """
    Zwraca:
    - aktualną fazę
    - pulę
    - czas do końca licytacji
    - gracza, który wygrał licytację (answering_player_id)
    - listę graczy (z is_admin)
    - czat (ostatnie 30 wpisów)
    """
    _cleanup_afk()
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


# --- CZAT -----------------------------------------------------------------


@app.post("/chat")
def post_chat(msg: ChatRequest):
    _cleanup_afk()
    CHAT.append(
        ChatMessage(
            player=msg.player,
            message=msg.message,
            timestamp=time.time(),
        )
    )
    if len(CHAT) > 200:
        del CHAT[:-200]
    return {"status": "ok"}


@app.get("/chat", response_model=List[ChatMessage])
def get_chat():
    _cleanup_afk()
    return CHAT[-50:]


# --- LEADERBOARD ----------------------------------------------------------


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
    _cleanup_afk()
    return LEADERBOARD[:50]
