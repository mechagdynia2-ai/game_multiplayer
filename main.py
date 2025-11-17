from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional, Set
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
    is_observer: bool


class BidInfo(BaseModel):
    player_id: str
    amount: int
    is_all_in: bool
    ts: float
    finished: bool = False  # czy gracz kliknął "Kończę licytację"


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

ROUND_ID: int = 0
PHASE: str = "waiting"  # "waiting" | "bidding" | "answering"

ROUND_START_TS: float = time.time()
BIDDING_DURATION: float = 20.0

POT: int = 0
ANSWERING_PLAYER_ID: Optional[str] = None

HEARTBEAT_TIMEOUT: float = 60.0
MAX_ACTIVE_PLAYERS: int = 20

ENTRY_FEE: int = 500        # min. kwota wejścia do rundy
MAX_BID_PER_ROUND: int = 5000  # maksymalna stawka znormalizowana (500 start + dobijanie do 5000)

# zbiór graczy, którzy kliknęli "Kończę licytację"
FINISHED_BIDDERS: Set[str] = set()


# --- FUNKCJE POMOCNICZE ---


def _bot_say(message: str) -> None:
    CHAT.append(
        ChatMessage(
            player="BOT",
            message=message,
            timestamp=time.time(),
        )
    )


def _recompute_pot() -> None:
    global POT
    POT = sum(b.amount for b in BIDS.values())


def _time_left() -> float:
    if PHASE != "bidding":
        return 0.0
    now = time.time()
    left = BIDDING_DURATION - (now - ROUND_START_TS)
    return max(0.0, left)


def _find_best_bid() -> Optional[BidInfo]:
    """
    Zwraca BidInfo z najwyższą stawką.
    Przy remisie wygrywa wcześniejszy timestamp.
    """
    best: Optional[BidInfo] = None
    for bid in BIDS.values():
        if best is None:
            best = bid
        else:
            if bid.amount > best.amount:
                best = bid
            elif bid.amount == best.amount and bid.ts < best.ts:
                best = bid
    return best


def _finish_bidding(trigger: str) -> None:
    """
    Zakończenie licytacji:
    - wybiera zwycięzcę,
    - ustawia PHASE="answering",
    - wysyła komunikat BOT na czat.
    """
    global PHASE, ANSWERING_PLAYER_ID

    if PHASE != "bidding":
        return

    best = _find_best_bid()
    if best is None:
        ANSWERING_PLAYER_ID = None
        PHASE = "answering"
        _bot_say(f"Licytacja zakończona ({trigger}). Nikt nie licytował.")
        return

    ANSWERING_PLAYER_ID = best.player_id
    PHASE = "answering"
    player = PLAYERS.get(best.player_id)
    name = player.name if player else "???"
    _bot_say(
        f"Licytacja zakończona ({trigger}). Gracz {name} wygrywa licytację "
        f"i odpowiada na pytanie. Pula: {POT} zł."
    )


def _auto_finish_if_needed() -> None:
    if PHASE == "bidding" and _time_left() <= 0:
        _finish_bidding(trigger="timer")


def _cleanup_inactive_players() -> None:
    """
    Usuwanie graczy, którzy są nieaktywni (brak heartbeat).
    Jeśli admin zniknie – wyznacz nowego admina.
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
            _bot_say(f"Gracz {player.name} opuścił grę (brak połączenia).")

    # Jeśli nie ma admina, a są gracze -> wyznacz nowego
    if PLAYERS and not any(p.is_admin for p in PLAYERS.values()):
        first_pid = next(iter(PLAYERS))
        PLAYERS[first_pid].is_admin = True
        _bot_say(f"Gracz {PLAYERS[first_pid].name} został nowym ADMINEM.")


def _start_new_round() -> None:
    """
    Nowa runda:
    - zwiększa ROUND_ID,
    - ustawia PHASE="bidding",
    - pobiera ENTRY_FEE od graczy z min. 500 zł,
    - tworzy początkowe stawki (po 500 zł),
    - graczy z kasą < 500 oznacza jako obserwatorów.
    """
    global ROUND_ID, PHASE, ROUND_START_TS, POT, ANSWERING_PLAYER_ID, BIDS, FINISHED_BIDDERS

    ROUND_ID += 1
    PHASE = "bidding"
    ROUND_START_TS = time.time()
    POT = 0
    ANSWERING_PLAYER_ID = None
    BIDS = {}
    FINISHED_BIDDERS = set()

    active_players = [p for p in PLAYERS.values() if not p.is_observer]
    now = time.time()

    if len(active_players) < 2:
        _bot_say("Za mało graczy z pełnym udziałem (min. 2). Runda nie została rozpoczęta.")
        PHASE = "waiting"
        return

    for p in active_players:
        if p.money < ENTRY_FEE:
            p.is_observer = True
            _bot_say(
                f"Gracz {p.name} ma mniej niż {ENTRY_FEE} zł "
                f"i staje się obserwatorem."
            )
            continue

        # pobieramy 500 zł i tworzymy początkową stawkę
        p.money -= ENTRY_FEE
        BIDS[p.id] = BidInfo(
            player_id=p.id,
            amount=ENTRY_FEE,
            is_all_in=False,
            ts=now,
            finished=False,
        )

    _recompute_pot()

    if not BIDS:
        _bot_say("Żaden gracz nie miał wystarczających środków. Runda nie wystartowała.")
        PHASE = "waiting"
        return

    _bot_say(
        f"Start rundy #{ROUND_ID}! Każdy gracz wniósł po {ENTRY_FEE} zł "
        f"do puli. Pula startowa: {POT} zł. Macie {int(BIDDING_DURATION)} s na licytację."
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
    Pierwszy gracz zostaje ADMINEM.
    Po przekroczeniu MAX_ACTIVE_PLAYERS – nowi są obserwatorami.
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
        _bot_say(f"Gracz {player.name} dołączył jako ADMIN.")
        _bot_say(
            "ADMINIE, wpisz numer zestawu pytań na czacie (01–50), "
            "aby rozpocząć grę."
        )
    elif is_observer:
        _bot_say(f"Gracz {player.name} dołączył jako obserwator.")
    else:
        _bot_say(f"Gracz {player.name} dołączył do gry.")

    # Jeśli po dołączeniu są przynajmniej 2 nieobserwujący gracze – BOT informuje
    active_players = [p for p in PLAYERS.values() if not p.is_observer]
    if len(active_players) == 2:
        _bot_say("Dołączyło 2 graczy – możemy zaczynać grę multiplayer!")

    return player


@app.get("/players", response_model=List[Player])
def list_players():
    return list(PLAYERS.values())


@app.post("/heartbeat")
def heartbeat(req: HeartbeatRequest):
    """
    Utrzymanie połączenia.
    Front wysyła co ~10 s {player_id}.
    Zwracamy informację, czy gracz jest adminem, obserwatorem itp.
    """
    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nie ma takiego gracza.")

    now = time.time()
    player = PLAYERS[req.player_id]
    player.last_heartbeat = now

    _cleanup_inactive_players()

    player = PLAYERS.get(req.player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Gracz został usunięty.")

    return {
        "status": "ok",
        "is_admin": player.is_admin,
        "is_observer": player.is_observer,
        "money": player.money,
    }


@app.post("/bid")
def place_bid(req: BidRequest):
    """
    Licytacja:
    - kind = "normal" -> +100 zł (jeśli gracz ma kasę i nie przekracza MAX_BID_PER_ROUND),
    - kind = "allin" -> VA BANQUE: wrzuca całą kasę, natychmiast kończy licytację.
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
    current_bid = BIDS.get(req.player_id)

    if req.kind == "normal":
        cost = 100
        if player.money < cost:
            raise HTTPException(
                status_code=400,
                detail="Za mało kasy na licytację +100.",
            )

        new_amount = (current_bid.amount if current_bid else 0) + cost
        # limit max 5000 zł w tej rundzie (bez VA BANQUE)
        if new_amount > MAX_BID_PER_ROUND:
            raise HTTPException(
                status_code=400,
                detail=f"Limit licytacji w tej rundzie to {MAX_BID_PER_ROUND} zł.",
            )

        player.money -= cost

        if current_bid:
            current_bid.amount = new_amount
            current_bid.ts = now
        else:
            BIDS[req.player_id] = BidInfo(
                player_id=req.player_id,
                amount=new_amount,
                is_all_in=False,
                ts=now,
                finished=False,
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

        if current_bid:
            current_bid.amount += add_amount
            current_bid.is_all_in = True
            current_bid.ts = now
        else:
            BIDS[req.player_id] = BidInfo(
                player_id=req.player_id,
                amount=add_amount,
                is_all_in=True,
                ts=now,
                finished=False,
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
    „Kończę licytację”:
    - jeśli ADMIN woła -> natychmiast kończymy,
    - jeśli zwykły gracz -> odkładamy flagę; gdy wszyscy aktywni licytujący
      zakończyli -> kończymy licytację.
    """
    global FINISHED_BIDDERS

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nie ma takiego gracza.")

    _auto_finish_if_needed()

    if PHASE != "bidding":
        raise HTTPException(
            status_code=400,
            detail="Licytacja już została zakończona.",
        )

    player = PLAYERS[req.player_id]

    # Admin może zawsze wymusić koniec licytacji
    if player.is_admin:
        _finish_bidding(trigger="admin")
        return {
            "status": "ok",
            "phase": PHASE,
            "answering_player_id": ANSWERING_PLAYER_ID,
            "pot": POT,
            "finished_by": "admin",
        }

    # zwykły gracz -> zaznaczamy, że zakończył licytację
    FINISHED_BIDDERS.add(req.player_id)
    if req.player_id in BIDS:
        BIDS[req.player_id].finished = True

    # sprawdzamy, czy wszyscy nieobserwujący, którzy mają stawkę, zakończyli
    active_bidders = [
        pid
        for pid, bid in BIDS.items()
        if not PLAYERS.get(pid, Player(id="", name="")).is_observer
        and bid.amount > 0
    ]

    all_finished = all(pid in FINISHED_BIDDERS for pid in active_bidders)

    if active_bidders and all_finished:
        _finish_bidding(trigger="all_players_finished")
        return {
            "status": "ok",
            "phase": PHASE,
            "answering_player_id": ANSWERING_PLAYER_ID,
            "pot": POT,
            "finished_by": "all_players",
        }

    return {
        "status": "ok",
        "phase": PHASE,
        "answering_player_id": ANSWERING_PLAYER_ID,
        "pot": POT,
        "finished_by": "partial",
    }


@app.post("/next_round")
def next_round():
    """
    Start nowej rundy – zwykle po wybraniu nowego pytania przez ADMINA
    (frontend może wywołać to np. po wpisaniu numeru zestawu
    i wysłaniu odpowiedniego komunikatu na czat).
    """
    _start_new_round()
    return {"status": "ok", "round_id": ROUND_ID, "phase": PHASE, "pot": POT}


@app.get("/state", response_model=StateResponse)
def get_state():
    """
    Aktualny stan gry do odświeżania frontendu:
    - runda, faza,
    - pula, czas do końca licytacji,
    - gracze,
    - czat (ostatnie 30 wiadomości).
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
                is_observer=p.is_observer,
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
    Zwykła wiadomość na czacie.
    Uwaga: logika interpretacji komend (np. ADMIN wpisuje „4” -> wybór zestawu)
    jest po stronie frontendu. Backend tylko przechowuje historię.
    """
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
