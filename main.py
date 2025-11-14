
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Optional
import time
import uuid

app = FastAPI(title="Awantura o Kasę – Multiplayer Backend")


class Player(BaseModel):
    id: str
    name: str
    money: int = 10000


class PlayerState(BaseModel):
    id: str
    name: str
    money: int
    bid: int
    is_all_in: bool


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


class StateResponse(BaseModel):
    round_id: int
    phase: str
    pot: int
    time_left: float
    answering_player_id: Optional[str]
    players: List[PlayerState]
    chat: List[ChatMessage]


PLAYERS: Dict[str, Player] = {}
BIDS: Dict[str, BidInfo] = {}
CHAT: List[ChatMessage] = []
LEADERBOARD: List[LeaderboardEntry] = []

ROUND_ID: int = 1
PHASE: str = "bidding"        # "bidding" | "answering"
ROUND_START_TS: float = time.time()
BIDDING_DURATION: float = 20.0
POT: int = 0
ANSWERING_PLAYER_ID: Optional[str] = None


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
    global PHASE
    if PHASE == "bidding" and _time_left() <= 0:
        _finish_bidding(trigger="timer")


def _finish_bidding(trigger: str) -> None:
    global PHASE, ANSWERING_PLAYER_ID

    if not BIDS:
        ANSWERING_PLAYER_ID = None
        PHASE = "answering"
        return

    best = None
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


@app.get("/")
def root():
    return {
        "message": "Awantura o Kasę Multiplayer – Backend działa 🎉",
        "docs": "/docs",
    }


@app.post("/register")
def register_player(req: RegisterRequest):
    player_id = str(uuid.uuid4())
    player = Player(id=player_id, name=req.name, money=10000)
    PLAYERS[player_id] = player
    return player


@app.get("/players", response_model=List[Player])
def list_players():
    return list(PLAYERS.values())


@app.post("/bid")
def place_bid(req: BidRequest):
    global PHASE

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nie ma takiego gracza.")

    _auto_finish_if_needed()

    if PHASE != "bidding":
        raise HTTPException(status_code=400, detail="Ta runda nie jest już w fazie licytacji.")

    player = PLAYERS[req.player_id]

    now = time.time()

    if req.kind == "normal":
        cost = 100
        if player.money < cost:
            raise HTTPException(status_code=400, detail="Za mało kasy na licytację +100.")
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
            raise HTTPException(status_code=400, detail="Nie możesz iść va banque z 0 zł.")

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
    _start_new_round()
    return {"status": "ok", "round_id": ROUND_ID}


@app.get("/state", response_model=StateResponse)
def get_state():
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


@app.get("/leaderboard")
def get_leaderboard():
    return LEADERBOARD[:50]
