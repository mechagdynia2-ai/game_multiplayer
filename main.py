from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import time
import uuid

app = FastAPI(title="Awantura o Kasę – Multiplayer Backend")

# --- CORS: frontend na GitHub Pages i lokalnie ---
origins = [
    "https://mechagdynia2-ai.github.io",
    "https://mechagdynia2-ai.github.io/awantura_o_kase_multiplayer",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MODELE DANYCH ---


class Player(BaseModel):
    id: str
    name: str
    money: int = 10000
    is_admin: bool = False
    is_observer: bool = False
    last_heartbeat: float = 0.0


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
    ts: float


class ChatMessage(BaseModel):
    player: str
    message: str
    timestamp: float


class RegisterRequest(BaseModel):
    name: str


class HeartbeatRequest(BaseModel):
    player_id: str


class BidRequest(BaseModel):
    player_id: str
    kind: str  # "normal" albo "allin"


class FinishBiddingRequest(BaseModel):
    player_id: str


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


class StateResponse(BaseModel):
    round_id: int
    phase: str
    pot: int
    time_left: float
    answering_player_id: Optional[str]
    players: List[PlayerState]
    chat: List[ChatMessage]


# --- STAN SERWERA ---

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

HEARTBEAT_TIMEOUT: float = 60.0  # po tylu sekundach uznajemy gracza za rozłączonego
MAX_ACTIVE_PLAYERS: int = 20     # powyżej tego nowi mogą być obserwatorami


# --- FUNKCJE POMOCNICZE ---


def _recompute_pot() -> None:
    """Przelicz pulę na podstawie BIDS."""
    global POT
    POT = sum(b.amount for b in BIDS.values())


def _time_left() -> float:
    """Ile sekund pozostało do końca licytacji."""
    if PHASE != "bidding":
        return 0.0
    now = time.time()
    left = BIDDING_DURATION - (now - ROUND_START_TS)
    return max(0.0, left)


def _finish_bidding(trigger: str) -> None:
    """
    Zakończ licytację:
    - wybierz gracza z najwyższą stawką (przy remisie wygrywa wcześniejszy czas).
    - przełącz PHASE na 'answering'.
    """
    global PHASE, ANSWERING_PLAYER_ID

    if PHASE != "bidding":
        return

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


def _auto_finish_if_needed() -> None:
    """Jeśli czas licytacji minął, automatycznie zakończ licytację."""
    if PHASE == "bidding" and _time_left() <= 0:
        _finish_bidding(trigger="timer")


def _start_new_round() -> None:
    """Nowa runda – reset licytacji, nowy ROUND_ID."""
    global ROUND_ID, PHASE, ROUND_START_TS, POT, ANSWERING_PLAYER_ID, BIDS
    ROUND_ID += 1
    PHASE = "bidding"
    ROUND_START_TS = time.time()
    POT = 0
    ANSWERING_PLAYER_ID = None
    BIDS = {}


def _cleanup_inactive_players() -> None:
    """
    Usuwanie graczy, którzy nie wysyłali heartbeat przez dłużej niż HEARTBEAT_TIMEOUT.
    Jeśli admin zniknie – wyznacz nowego admina (pierwszy z listy).
    """
    global PLAYERS, BIDS

    now = time.time()
    removed_ids = []
    for pid, p in list(PLAYERS.items()):
        if now - p.last_heartbeat > HEARTBEAT_TIMEOUT:
            removed_ids.append(pid)

    for pid in removed_ids:
        player = PLAYERS.pop(pid, None)
        BIDS.pop(pid, None)
        if player:
            CHAT.append(
                ChatMessage(
                    player="BOT",
                    message=f"Gracz {player.name} został odłączony (brak heartbeat).",
                    timestamp=time.time(),
                )
            )

    # Jeśli nie ma admina, a są gracze -> wyznacz nowego
    if PLAYERS and not any(p.is_admin for p in PLAYERS.values()):
        # pierwszy z istniejących staje się adminem
        first_pid = next(iter(PLAYERS))
        PLAYERS[first_pid].is_admin = True
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=f"Gracz {PLAYERS[first_pid].name} został nowym ADMINEM.",
                timestamp=time.time(),
            )
        )


# --- ENDPOINTY ---


@app.get("/")
def root():
    return {
        "message": "Awantura o Kasę – Multiplayer Backend działa 🎉",
        "docs": "/docs",
    }


@app.post("/register", response_model=Player)
def register_player(req: RegisterRequest):
    """
    Rejestracja gracza.
    Pierwszy gracz na serwerze zostaje ADMINEM.
    Jeśli graczy jest więcej niż MAX_ACTIVE_PLAYERS – nowi mogą być obserwatorami.
    """
    global PLAYERS

    now = time.time()
    player_id = str(uuid.uuid4())

    is_admin = len(PLAYERS) == 0
    is_observer = len(PLAYERS) >= MAX_ACTIVE_PLAYERS

    player = Player(
        id=player_id,
        name=req.name,
        money=10000,
        is_admin=is_admin,
        is_observer=is_observer,
        last_heartbeat=now,
    )
    PLAYERS[player_id] = player

    if is_admin:
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=f"Gracz {player.name} dołączył jako ADMIN.",
                timestamp=now,
            )
        )
    else:
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=f"Gracz {player.name} dołączył do gry.",
                timestamp=now,
            )
        )

    return player


@app.get("/players", response_model=List[Player])
def list_players():
    return list(PLAYERS.values())


@app.post("/heartbeat")
def heartbeat(req: HeartbeatRequest):
    """
    Utrzymanie połączenia gracza.
    Frontend wysyła co ok. 10 sekund {player_id: "..."}.
    Zwracamy informację, czy gracz jest ADMINEM.
    """
    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nie ma takiego gracza.")

    now = time.time()
    player = PLAYERS[req.player_id]
    player.last_heartbeat = now

    # Wyczyść nieaktywnych (w tym może zniknąć dotychczasowy admin)
    _cleanup_inactive_players()

    # Po cleanupie, player może zostać adminem (np. gdy był jedynym w pokoju)
    is_admin_now = PLAYERS.get(req.player_id, player).is_admin

    return {"status": "ok", "is_admin": is_admin_now}


@app.post("/bid")
def place_bid(req: BidRequest):
    """
    Licytacja:
    - kind = "normal" -> +100 zł (jeśli gracz ma kasę)
    - kind = "allin" -> wrzuca całą kasę, natychmiast kończy licytację
    """
    global PHASE

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nie ma takiego gracza.")

    _auto_finish_if_needed()

    if PHASE != "bidding":
        raise HTTPException(
            status_code=400,
            detail="Ta runda nie jest już w fazie licytacji.",
        )

    player = PLAYERS[req.player_id]

    if player.is_observer:
        raise HTTPException(
            status_code=400,
            detail="Obserwator nie może licytować.",
        )

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
                detail="Nie możesz iść VA BANQUE z 0 zł.",
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
        return {
            "status": "ok",
            "pot": POT,
            "phase": PHASE,
            "answering_player_id": ANSWERING_PLAYER_ID,
        }

    else:
        raise HTTPException(status_code=400, detail="Nieznany rodzaj licytacji.")


@app.post("/finish_bidding")
def finish_bidding(req: FinishBiddingRequest):
    """
    Ręczne zakończenie licytacji – tylko ADMIN może to zrobić.
    Używane z przycisku „Kończę licytację”.
    """
    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nie ma takiego gracza.")

    player = PLAYERS[req.player_id]
    if not player.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Tylko ADMIN może zakończyć licytację.",
        )

    _finish_bidding(trigger="admin")
    return {
        "status": "ok",
        "phase": PHASE,
        "answering_player_id": ANSWERING_PLAYER_ID,
        "pot": POT,
    }


@app.post("/next_round")
def next_round():
    """
    Start nowej rundy – zwykle wołane po wybraniu nowego zestawu pytań przez ADMINA.
    """
    _start_new_round()
    return {"status": "ok", "round_id": ROUND_ID}


@app.get("/state", response_model=StateResponse)
def get_state():
    """
    Zwraca aktualny stan:
    - tura, faza, czas do końca licytacji
    - graczy wraz z ich stawkami i kasą
    - czat (ostatnie ~30 wiadomości)
    """
    _auto_finish_if_needed()
    _cleanup_inactive_players()

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


@app.get("/leaderboard", response_model=List[LeaderboardEntry])
def get_leaderboard():
    return LEADERBOARD[:50]
