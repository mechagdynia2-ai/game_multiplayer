from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import time
import uuid

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://mechagdynia2-ai.github.io",
        "https://mechagdynia2-ai.github.io/",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app = FastAPI(title="Awantura o Kasę – Multiplayer Backend (lobby + 1 gra)")


# ------------------------------------------------------------
#   CORS
# ------------------------------------------------------------

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
#   MODELE
# ------------------------------------------------------------

class Player(BaseModel):
    id: str
    name: str
    money: int = 10000
    is_admin: bool = False        # admin lobby / gry
    is_observer: bool = False     # nie może licytować
    last_seen: float = 0.0
    lobby_id: Optional[str] = None
    in_game: bool = False
    last_game_id: int = 0         # do priorytetu "kto nie grał ostatnio"


class PlayerState(BaseModel):
    id: str
    name: str
    money: int
    bid: int
    is_all_in: bool
    is_admin: bool
    is_observer: bool
    in_game: bool
    lobby_id: Optional[str]


class BidInfo(BaseModel):
    player_id: str
    amount: int
    is_all_in: bool
    ts: float


class ChatMessage(BaseModel):
    player: str   # "BOT" albo nick gracza
    message: str
    timestamp: float


class Lobby(BaseModel):
    id: str
    created_ts: float


class LobbySummary(BaseModel):
    id: str
    player_count: int
    has_admin: bool


class RegisterRequest(BaseModel):
    name: str


class HeartbeatRequest(BaseModel):
    player_id: str


class ChatRequest(BaseModel):
    player: str
    message: str


class BidRequest(BaseModel):
    player_id: str
    kind: str  # "normal" | "allin"


class FinishBiddingRequest(BaseModel):
    player_id: str  # kto manualnie kończy licytację


class StartGameRequest(BaseModel):
    admin_id: str
    question_set: str  # np. "01", "15" itd.


class EndGameRequest(BaseModel):
    winner_id: Optional[str] = None
    reason: Optional[str] = None


class StateResponse(BaseModel):
    round_id: int
    phase: str
    pot: int
    time_left: float
    answering_player_id: Optional[str]
    game_active: bool
    game_id: int
    question_set: Optional[str]
    players: List[PlayerState]
    chat: List[ChatMessage]


# ------------------------------------------------------------
#   STAN SERWERA
# ------------------------------------------------------------

PLAYERS: Dict[str, Player] = {}
BIDS: Dict[str, BidInfo] = {}
CHAT: List[ChatMessage] = []
LOBBIES: Dict[str, Lobby] = {}

ROUND_ID: int = 1
PHASE: str = "bidding"  # "bidding" | "answering"
ROUND_START_TS: float = time.time()
BIDDING_DURATION: float = 20.0
POT: int = 0
ANSWERING_PLAYER_ID: Optional[str] = None

AFK_TIMEOUT: float = 30.0

MAX_LOBBIES: int = 5
MAX_GAME_PLAYERS: int = 6

GAME_ACTIVE: bool = False
GAME_ID: int = 0
CURRENT_QUESTION_SET: Optional[str] = None


# ------------------------------------------------------------
#   FUNKCJE POMOCNICZE
# ------------------------------------------------------------

def _touch(player_id: str) -> None:
    p = PLAYERS.get(player_id)
    if p:
        p.last_seen = time.time()


def _recompute_pot() -> None:
    global POT
    POT = sum(b.amount for b in BIDS.values())


def _time_left() -> float:
    if not GAME_ACTIVE or PHASE != "bidding":
        return 0.0
    now = time.time()
    left = BIDDING_DURATION - (now - ROUND_START_TS)
    return max(0.0, left)


def _auto_finish_if_needed() -> None:
    if GAME_ACTIVE and PHASE == "bidding" and _time_left() <= 0:
        _finish_bidding(trigger="timer")


def _finish_bidding(trigger: str = "manual") -> None:
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

    ANSWERING_PLAYER_ID = best.player_id
    PHASE = "answering"


def _start_new_round() -> None:
    global ROUND_ID, PHASE, ROUND_START_TS, POT, ANSWERING_PLAYER_ID, BIDS
    ROUND_ID += 1
    PHASE = "bidding"
    ROUND_START_TS = time.time()
    POT = 0
    ANSWERING_PLAYER_ID = None
    BIDS = {}


def _cleanup_afk() -> None:
    """
    Usuwamy graczy, którzy nie wysłali heartbeat od AFK_TIMEOUT sekund.
    Jeśli lobby się opróżni → usuwamy lobby.
    """
    now = time.time()
    to_remove: List[str] = []

    for pid, p in list(PLAYERS.items()):
        if now - p.last_seen > AFK_TIMEOUT:
            to_remove.append(pid)

    for pid in to_remove:
        player = PLAYERS.get(pid)
        if not player:
            continue
        name = player.name
        lobby_id = player.lobby_id
        del PLAYERS[pid]
        if pid in BIDS:
            del BIDS[pid]
        if pid == ANSWERING_PLAYER_ID:
            _reset_answering_player()
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=f"{name} został usunięty (AFK).",
                timestamp=time.time(),
            )
        )
        # później posprzątamy lobby

    # usuwanie pustych lobby + ewentualne przekazanie admina
    _reassign_admins_and_cleanup_lobbies()
    _recompute_pot()


def _reset_answering_player() -> None:
    global ANSWERING_PLAYER_ID
    ANSWERING_PLAYER_ID = None


def _reassign_admins_and_cleanup_lobbies() -> None:
    """
    - jeśli lobby nie ma żadnych graczy → usuń
    - jeśli ma graczy, ale żaden nie jest adminem → ustaw admina (pierwszy z listy)
    """
    for lobby_id in list(LOBBIES.keys()):
        players_in_lobby = [
            p for p in PLAYERS.values() if p.lobby_id == lobby_id and not p.in_game
        ]
        if not players_in_lobby:
            # lobby puste → usuń
            del LOBBIES[lobby_id]
            continue

        if not any(p.is_admin for p in players_in_lobby):
            # brak admina → pierwszy zostaje adminem
            players_in_lobby.sort(key=lambda x: x.last_seen)
            new_admin = players_in_lobby[0]
            new_admin.is_admin = True
            CHAT.append(
                ChatMessage(
                    player="BOT",
                    message=f"{new_admin.name} został nowym ADMINEM w lobby {lobby_id}.",
                    timestamp=time.time(),
                )
            )


def _get_lobby_players(lobby_id: str) -> List[Player]:
    return [
        p
        for p in PLAYERS.values()
        if p.lobby_id == lobby_id and not p.in_game and not p.is_observer
    ]


def _get_all_lobby_players() -> List[Player]:
    return [
        p
        for p in PLAYERS.values()
        if p.lobby_id is not None and not p.in_game and not p.is_observer
    ]


def _select_players_for_game(base_lobby_id: str, question_set: str) -> List[Player]:
    """
    Logika wyboru max 6 graczy:
    - najpierw gracze z lobby admina
    - potem gracze z innych lobby
    - w każdej grupie priorytet:
        1. nie-admin
        2. tacy, którzy NIE grali w poprzedniej grze
        3. admini
    - admini wchodzą na końcu, jeśli są wolne sloty
    """
    global GAME_ID

    base_players = _get_lobby_players(base_lobby_id)
    other_players = [
        p
        for p in _get_all_lobby_players()
        if p.lobby_id != base_lobby_id
    ]

    def sort_key(p: Player):
        played_last = (p.last_game_id == GAME_ID)  # grał w poprzedniej grze?
        return (
            p.is_admin,          # admini później
            played_last,         # ci, co grali ostatnio → później
            p.last_seen,         # starsi (wcześniej aktywni) wcześniej
        )

    base_players_sorted = sorted(base_players, key=sort_key)
    other_players_sorted = sorted(other_players, key=sort_key)

    selected: List[Player] = []

    # Najpierw z lobby admina
    for p in base_players_sorted:
        if len(selected) >= MAX_GAME_PLAYERS:
            break
        selected.append(p)

    # Potem z innych lobby
    if len(selected) < MAX_GAME_PLAYERS:
        for p in other_players_sorted:
            if len(selected) >= MAX_GAME_PLAYERS:
                break
            selected.append(p)

    return selected


def _start_game_session(players: List[Player], question_set: str) -> None:
    """
    Ustawia stan na start nowej gry:
    - oznacza graczy jako in_game
    - ustawia last_game_id
    - resetuje licytację
    """
    global GAME_ACTIVE, GAME_ID, CURRENT_QUESTION_SET
    global ROUND_ID, PHASE, ROUND_START_TS, POT, ANSWERING_PLAYER_ID, BIDS

    GAME_ID += 1
    GAME_ACTIVE = True
    CURRENT_QUESTION_SET = question_set

    ROUND_ID = 1
    PHASE = "bidding"
    ROUND_START_TS = time.time()
    POT = 0
    ANSWERING_PLAYER_ID = None
    BIDS = {}

    for p in PLAYERS.values():
        p.in_game = False  # na wszelki wypadek

    names = []
    for p in players:
        p.in_game = True
        p.last_game_id = GAME_ID
        names.append(p.name)

    CHAT.append(
        ChatMessage(
            player="BOT",
            message=(
                f"Start gry #{GAME_ID} (zestaw {CURRENT_QUESTION_SET}). "
                f"Gracze: {', '.join(names)}"
            ),
            timestamp=time.time(),
        )
    )


def _end_game_session(winner_id: Optional[str], reason: Optional[str]) -> None:
    """
    Kończy aktualną grę:
    - zaznacza graczy jako in_game=False
    - przenosi ich z powrotem do lobby (zachowujemy lobby_id)
    - jeśli w lobby brakuje admina → ktoś zostaje adminem
    """
    global GAME_ACTIVE, CURRENT_QUESTION_SET, POT, ANSWERING_PLAYER_ID, BIDS

    if not GAME_ACTIVE:
        return

    GAME_ACTIVE = False
    CURRENT_QUESTION_SET = None
    POT = 0
    ANSWERING_PLAYER_ID = None
    BIDS = {}

    winner_name = None
    if winner_id and winner_id in PLAYERS:
        winner_name = PLAYERS[winner_id].name

    if winner_name:
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=f"Gra zakończona. Zwycięzca: {winner_name}. Powód: {reason or '-'}",
                timestamp=time.time(),
            )
        )
    else:
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=f"Gra zakończona. Powód: {reason or '-'}",
                timestamp=time.time(),
            )
        )

    # Gracze wracają do lobby (in_game=False)
    for p in PLAYERS.values():
        if p.in_game:
            p.in_game = False

    _reassign_admins_and_cleanup_lobbies()


# ------------------------------------------------------------
#   ENDPOINTY
# ------------------------------------------------------------

@app.get("/")
def root():
    return {
        "message": "Awantura o Kasę – Backend (lobby + 1 gra) działa 🎉",
        "docs": "/docs",
    }


# ------------------------------------------------------------
#   REJESTRACJA GRACZA
# ------------------------------------------------------------

@app.post("/register", response_model=Player)
def register_player(req: RegisterRequest):
    player_id = str(uuid.uuid4())

    # Nowy gracz nie jest w żadnym lobby, nie jest adminem, nie jest obserwatorem
    p = Player(
        id=player_id,
        name=req.name,
        money=10000,
        is_admin=False,
        is_observer=False,
        last_seen=time.time(),
        lobby_id=None,
        in_game=False,
        last_game_id=0,
    )
    PLAYERS[player_id] = p

    CHAT.append(
        ChatMessage(
            player="BOT",
            message=f"{req.name} dołączył do serwera.",
            timestamp=time.time(),
        )
    )

    return p


# ------------------------------------------------------------
#   HEARTBEAT
# ------------------------------------------------------------

@app.post("/heartbeat")
def heartbeat(req: HeartbeatRequest):
    _cleanup_afk()

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Gracz zniknął lub został usunięty.")

    PLAYERS[req.player_id].last_seen = time.time()
    return {
        "status": "ok",
        "is_admin": PLAYERS[req.player_id].is_admin,
        "in_game": PLAYERS[req.player_id].in_game,
        "lobby_id": PLAYERS[req.player_id].lobby_id,
    }


# ------------------------------------------------------------
#   LOBBY: tworzenie / lista / dołączanie / opuszczanie
# ------------------------------------------------------------

@app.get("/lobbies", response_model=List[LobbySummary])
def list_lobbies():
    _cleanup_afk()
    summaries: List[LobbySummary] = []

    for lobby_id, lobby in LOBBIES.items():
        players_in_lobby = [
            p for p in PLAYERS.values() if p.lobby_id == lobby_id and not p.in_game
        ]
        if not players_in_lobby:
            continue
        has_admin = any(p.is_admin for p in players_in_lobby)
        summaries.append(
            LobbySummary(
                id=lobby_id,
                player_count=len(players_in_lobby),
                has_admin=has_admin,
            )
        )

    return summaries


@app.post("/lobby/create")
def create_lobby(req: HeartbeatRequest):
    """
    Tworzy nowe lobby; gracz zostaje jego ADMINEM.
    Max 5 lobby.
    """
    _cleanup_afk()

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Gracz nie istnieje.")

    if len(LOBBIES) >= MAX_LOBBIES:
        raise HTTPException(status_code=400, detail="Osiągnięto limit lobby (5).")

    player = PLAYERS[req.player_id]
    if player.in_game:
        raise HTTPException(status_code=400, detail="Jesteś w grze – nie możesz tworzyć lobby.")

    # jeśli był adminem innego lobby, zostawiamy to, ale zmieniamy lobby_id
    lobby_id = f"L{len(LOBBIES) + 1}"
    LOBBIES[lobby_id] = Lobby(id=lobby_id, created_ts=time.time())

    player.lobby_id = lobby_id
    player.is_admin = True

    CHAT.append(
        ChatMessage(
            player="BOT",
            message=f"{player.name} założył pokój gry (lobby {lobby_id}).",
            timestamp=time.time(),
        )
    )

    return {"status": "ok", "lobby_id": lobby_id}


class JoinLobbyRequest(BaseModel):
    player_id: str
    lobby_id: str


@app.post("/lobby/join")
def join_lobby(req: JoinLobbyRequest):
    _cleanup_afk()

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Gracz nie istnieje.")
    if req.lobby_id not in LOBBIES:
        raise HTTPException(status_code=404, detail="Lobby nie istnieje.")

    player = PLAYERS[req.player_id]
    if player.in_game:
        raise HTTPException(status_code=400, detail="Jesteś w grze – nie możesz wejść do lobby.")

    old_lobby_id = player.lobby_id
    player.lobby_id = req.lobby_id
    # jeśli był adminem starego lobby, straci admina – nowy zostanie przydzielony
    if old_lobby_id and old_lobby_id != req.lobby_id and player.is_admin:
        player.is_admin = False

    CHAT.append(
        ChatMessage(
            player="BOT",
            message=f"{player.name} dołączył do lobby {req.lobby_id}.",
            timestamp=time.time(),
        )
    )

    _reassign_admins_and_cleanup_lobbies()

    return {"status": "ok", "lobby_id": req.lobby_id}


@app.post("/lobby/leave")
def leave_lobby(req: HeartbeatRequest):
    _cleanup_afk()

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Gracz nie istnieje.")

    player = PLAYERS[req.player_id]
    lobby_id = player.lobby_id

    if not lobby_id:
        return {"status": "ok"}  # i tak nie jest w lobby

    player.lobby_id = None
    was_admin = player.is_admin
    player.is_admin = False

    CHAT.append(
        ChatMessage(
            player="BOT",
            message=f"{player.name} opuścił lobby {lobby_id}.",
            timestamp=time.time(),
        )
    )

    if was_admin:
        _reassign_admins_and_cleanup_lobbies()

    return {"status": "ok"}


# ------------------------------------------------------------
#   START / KONIEC GRY
# ------------------------------------------------------------

@app.post("/game/start")
def start_game(req: StartGameRequest):
    """
    Admin lobby startuje nową grę:
    - bierze graczy ze swojego lobby
    - dopełnia graczami z innych lobby do max 6
    - nikt nie może dołączyć w trakcie gry
    """
    global GAME_ACTIVE

    _cleanup_afk()

    if GAME_ACTIVE:
        raise HTTPException(status_code=400, detail="Gra jest już w toku.")

    if req.admin_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Admin nie istnieje.")

    admin = PLAYERS[req.admin_id]
    if not admin.lobby_id:
        raise HTTPException(status_code=400, detail="Najpierw dołącz lub stwórz lobby.")
    if not admin.is_admin:
        raise HTTPException(status_code=403, detail="Nie jesteś adminem tego lobby.")

    base_lobby_id = admin.lobby_id
    selected_players = _select_players_for_game(base_lobby_id, req.question_set)

    if len(selected_players) < 2:
        raise HTTPException(
            status_code=400,
            detail="Do rozpoczęcia gry multiplayer potrzebnych jest min. 2 graczy.",
        )

    _start_game_session(selected_players, req.question_set)

    CHAT.append(
        ChatMessage(
            player="BOT",
            message=(
                f"Gra multiplayer wystartowała (zestaw {req.question_set}). "
                "Nowi gracze nie mogą dołączyć, dopóki gra trwa."
            ),
            timestamp=time.time(),
        )
    )

    return {
        "status": "ok",
        "game_id": GAME_ID,
        "question_set": req.question_set,
        "players": [p.id for p in selected_players],
    }


@app.post("/game/end")
def end_game(req: EndGameRequest):
    """
    Wywoływane z frontu, gdy cała sesja gry się kończy
    (np. ostatnie pytanie, warunki zwycięstwa itd.).
    """
    _cleanup_afk()
    _end_game_session(req.winner_id, req.reason)
    return {"status": "ok"}


# ------------------------------------------------------------
#   CZAT
# ------------------------------------------------------------

@app.post("/chat")
def post_chat(req: ChatRequest):
    _cleanup_afk()

    CHAT.append(
        ChatMessage(
            player=req.player,
            message=req.message,
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


# ------------------------------------------------------------
#   LICYTACJA
# ------------------------------------------------------------

@app.post("/bid")
def bid(req: BidRequest):
    global PHASE

    _cleanup_afk()
    _auto_finish_if_needed()

    if not GAME_ACTIVE:
        raise HTTPException(status_code=400, detail="Żadna gra nie jest aktualnie w toku.")

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nieznany gracz.")

    player = PLAYERS[req.player_id]

    if player.is_observer or not player.in_game:
        raise HTTPException(status_code=400, detail="Nie możesz licytować w tej grze.")

    if PHASE != "bidding":
        raise HTTPException(status_code=400, detail="Licytacja jest już zamknięta.")

    now = time.time()

    if req.kind == "normal":
        cost = 100
        if player.money < cost:
            raise HTTPException(status_code=400, detail="Nie masz 100 zł na postawienie.")
        player.money -= cost

        if req.player_id not in BIDS:
            BIDS[req.player_id] = BidInfo(
                player_id=req.player_id,
                amount=cost,
                is_all_in=False,
                ts=now,
            )
        else:
            BIDS[req.player_id].amount += cost
            BIDS[req.player_id].ts = now

        _recompute_pot()

        CHAT.append(
            ChatMessage(
                player=player.name,
                message=f"licytuje +100 zł (łącznie {BIDS[req.player_id].amount} zł)",
                timestamp=time.time(),
            )
        )

        return {"status": "ok", "pot": POT}

    elif req.kind == "allin":
        if player.money <= 0:
            raise HTTPException(status_code=400, detail="Nie możesz iść va banque z 0 zł.")

        add_amount = player.money
        player.money = 0

        if req.player_id not in BIDS:
            BIDS[req.player_id] = BidInfo(
                player_id=req.player_id,
                amount=add_amount,
                is_all_in=True,
                ts=now,
            )
        else:
            BIDS[req.player_id].amount += add_amount
            BIDS[req.player_id].is_all_in = True
            BIDS[req.player_id].ts = now

        _recompute_pot()
        _finish_bidding(trigger="allin")

        CHAT.append(
            ChatMessage(
                player=player.name,
                message=f"poszedł VA BANQUE ({BIDS[req.player_id].amount} zł)!",
                timestamp=time.time(),
            )
        )

        return {"status": "ok", "pot": POT, "phase": PHASE}

    else:
        raise HTTPException(status_code=400, detail="Nieznany rodzaj licytacji.")


@app.post("/finish_bidding")
def finish_bidding(req: FinishBiddingRequest):
    """
    Manualne zakończenie licytacji („Kończę licytację”).
    """
    _cleanup_afk()

    if not GAME_ACTIVE:
        raise HTTPException(status_code=400, detail="Gra nie jest w toku.")

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nieznany gracz.")

    player = PLAYERS[req.player_id]
    if player.is_observer or not player.in_game:
        raise HTTPException(status_code=400, detail="Nie możesz zakończyć licytacji.")

    _finish_bidding(trigger="manual")

    CHAT.append(
        ChatMessage(
            player=player.name,
            message="kończy licytację.",
            timestamp=time.time(),
        )
    )

    return {"status": "ok", "phase": PHASE}


# ------------------------------------------------------------
#   STAN GRY
# ------------------------------------------------------------

@app.get("/state", response_model=StateResponse)
def state():
    _cleanup_afk()
    _auto_finish_if_needed()

    players_state: List[PlayerState] = []
    for pid, p in PLAYERS.items():
        bid_amount = BIDS[pid].amount if pid in BIDS else 0
        is_all_in = BIDS[pid].is_all_in if pid in BIDS else False

        players_state.append(
            PlayerState(
                id=p.id,
                name=p.name,
                money=p.money,
                bid=bid_amount,
                is_all_in=is_all_in,
                is_admin=p.is_admin,
                is_observer=p.is_observer,
                in_game=p.in_game,
                lobby_id=p.lobby_id,
            )
        )

    chat_slice = CHAT[-30:]

    return StateResponse(
        round_id=ROUND_ID,
        phase=PHASE,
        pot=POT,
        time_left=_time_left(),
        answering_player_id=ANSWERING_PLAYER_ID,
        game_active=GAME_ACTIVE,
        game_id=GAME_ID,
        question_set=CURRENT_QUESTION_SET,
        players=players_state,
        chat=chat_slice,
    )

