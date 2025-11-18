from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import time
import uuid
import re
import difflib
from urllib.request import urlopen

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

GITHUB_QUESTIONS_BASE = "https://raw.githubusercontent.com/mechagdynia2-ai/game/main/assets/"

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
    current_set: Optional[str]
    current_question_index: int
    current_question_text: Optional[str]
    players: List[PlayerState]
    chat: List[ChatMessage]


class SelectSetRequest(BaseModel):
    player_id: str
    set_no: int  # 1–50


class AnswerRequest(BaseModel):
    player_id: str
    answer: str


class HintRequest(BaseModel):
    player_id: str
    kind: str  # "abcd" lub "5050"


# --- STAN SERWERA ---

PLAYERS: Dict[str, Player] = {}
BIDS: Dict[str, BidInfo] = {}
CHAT: List[ChatMessage] = []
LEADERBOARD: List[LeaderboardEntry] = []

ROUND_ID: int = 0
PHASE: str = "idle"  # "idle" | "bidding" | "answering" | "discussion" | "finished"
ROUND_START_TS: float = time.time()
BIDDING_DURATION: float = 20.0
POT: int = 0
ANSWERING_PLAYER_ID: Optional[str] = None

HEARTBEAT_TIMEOUT: float = 60.0  # po tylu sekundach uznajemy gracza za rozłączonego
MAX_ACTIVE_PLAYERS: int = 20

QUESTIONS: List[Dict] = []
CURRENT_SET: Optional[str] = None
CURRENT_Q_INDEX: int = -1

CURRENT_ANSWER_TEXT: Optional[str] = None
CURRENT_ANSWER_PLAYER_ID: Optional[str] = None
ANSWER_SUBMITTED_TS: float = 0.0


# --- FUNKCJE POMOCNICZE ---


def _normalize_answer(text: str) -> str:
    text = str(text).lower().strip()
    repl = {
        "ó": "o",
        "ł": "l",
        "ż": "z",
        "ź": "z",
        "ć": "c",
        "ń": "n",
        "ś": "s",
        "ą": "a",
        "ę": "e",
        "ü": "u",
    }
    for c, r in repl.items():
        text = text.replace(c, r)
    text = text.replace("u", "o")
    return "".join(text.split())


def _similarity(a: str, b: str) -> int:
    na = _normalize_answer(a)
    nb = _normalize_answer(b)
    if not na and not nb:
        return 100
    return int(difflib.SequenceMatcher(None, na, nb).ratio() * 100)


def _recompute_pot() -> None:
    global POT
    POT = sum(b.amount for b in BIDS.values())


def _time_left() -> float:
    """Ile sekund pozostało do końca licytacji."""
    if PHASE != "bidding":
        return 0.0
    now = time.time()
    left = BIDDING_DURATION - (now - ROUND_START_TS)
    return max(0.0, left)


def _load_question_set(set_no: int) -> List[Dict]:
    if set_no < 1 or set_no > 50:
        raise ValueError("set_no out of range")
    filename = f"{set_no:02d}.txt"
    url = GITHUB_QUESTIONS_BASE + filename
    with urlopen(url) as resp:
        content = resp.read().decode("utf-8", errors="ignore")

    parsed: List[Dict] = []
    blocks = re.split(r"\n(?=\d{1,3}\.)", content)
    for block in blocks:
        block = block.strip()
        if not block:
            continue

        q_match = re.match(r"^\d{1,3}\.\s*(.+)", block)
        if not q_match:
            continue
        question = q_match.group(1).strip()

        correct_match = re.search(
            r"prawidłowa\s+odpowied[zź]\s*=\s*(.+)",
            block,
            re.IGNORECASE,
        )
        if not correct_match:
            continue
        correct = correct_match.group(1).strip()

        answers_match = re.search(
            r"odpowied[zź]\s*abcd\s*=\s*A\s*=\s*(.+?),\s*B\s*=\s*(.+?),\s*C\s*=\s*(.+?),\s*D\s*=\s*(.+)",
            block,
            re.IGNORECASE,
        )
        if not answers_match:
            continue

        a = answers_match.group(1).strip()
        b = answers_match.group(2).strip()
        c = answers_match.group(3).strip()
        d = answers_match.group(4).strip()

        parsed.append(
            {
                "question": question,
                "correct": correct,
                "answers": [a, b, c, d],
            }
        )
    return parsed


def _cleanup_inactive_players() -> None:
    global PLAYERS, BIDS
    now = time.time()
    removed_ids = []
    for pid, p in list(PLAYERS.items()):
        if now - p.last_heartbeat > HEARTBEAT_TIMEOUT:
            removed_ids.append(pid)

    for pid in removed_ids:
        player = PLAYERS.pop(pid, None)
        if player:
            BIDS.pop(pid, None)
            CHAT.append(
                ChatMessage(
                    player="BOT",
                    message=f"Gracz {player.name} został odłączony (brak heartbeat).",
                    timestamp=time.time(),
                )
            )

    # po usunięciu przelicz pulę
    _recompute_pot()

    # Jeśli nie ma admina, wyznacz nowego
    if PLAYERS and not any(p.is_admin for p in PLAYERS.values()):
        first_pid = next(iter(PLAYERS))
        PLAYERS[first_pid].is_admin = True
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=f"Gracz {PLAYERS[first_pid].name} został nowym ADMINEM.",
                timestamp=time.time(),
            )
        )


def _finish_bidding(trigger: str) -> None:
    """
    Zakończ licytację:
    - wybierz gracza z najwyższą stawką (przy remisie wygrywa wcześniejszy czas).
    - przełącz PHASE na 'answering' i ustaw ANSWERING_PLAYER_ID.
    """
    global PHASE, ANSWERING_PLAYER_ID

    if PHASE != "bidding":
        return

    if not BIDS:
        ANSWERING_PLAYER_ID = None
        PHASE = "answering"
        CHAT.append(
            ChatMessage(
                player="BOT",
                message="Brak ofert w licytacji – brak osoby odpowiadającej.",
                timestamp=time.time(),
            )
        )
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

    if ANSWERING_PLAYER_ID and ANSWERING_PLAYER_ID in PLAYERS:
        winner = PLAYERS[ANSWERING_PLAYER_ID]
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=f"Gracz {winner.name} zwyciężył licytację z kwotą {best.amount} zł.",
                timestamp=time.time(),
            )
        )

    # Ogłoszenie pytania na czacie
    if 0 <= CURRENT_Q_INDEX < len(QUESTIONS):
        q = QUESTIONS[CURRENT_Q_INDEX]
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=f"PYTANIE: {q['question']}",
                timestamp=time.time(),
            )
        )


def _auto_finish_bidding_if_needed() -> None:
    if PHASE == "bidding" and _time_left() <= 0:
        _finish_bidding(trigger="timer")


def _end_game_no_more_questions() -> None:
    global PHASE
    PHASE = "finished"
    # wybierz zwycięzcę po kasie
    if not PLAYERS:
        CHAT.append(
            ChatMessage(
                player="BOT",
                message="Gra zakończona – brak graczy.",
                timestamp=time.time(),
            )
        )
        return

    active_players = [p for p in PLAYERS.values() if not p.is_observer]
    if not active_players:
        active_players = list(PLAYERS.values())

    winner = max(active_players, key=lambda p: p.money)
    CHAT.append(
        ChatMessage(
            player="BOT",
            message=f"Gra zakończona – zwycięża {winner.name} z kwotą {winner.money} zł.",
            timestamp=time.time(),
        )
    )


def _end_game_insufficient_players() -> None:
    global PHASE, POT
    PHASE = "finished"
    active = [p for p in PLAYERS.values() if not p.is_observer and p.money >= 500]
    if len(active) == 1:
        winner = active[0]
        winner.money += POT
        msg = (
            f"Gra zakończona – tylko {winner.name} ma min. 500 zł. "
            f"Zgarnia całą pulę {POT} zł i zwycięża z kwotą {winner.money} zł."
        )
        POT = 0
    else:
        msg = "Gra zakończona – zbyt mało graczy z min. 500 zł, aby kontynuować."
    CHAT.append(
        ChatMessage(
            player="BOT",
            message=msg,
            timestamp=time.time(),
        )
    )


def _start_new_bidding_round() -> None:
    """
    Rozpoczyna nową rundę licytacji dla aktualnego pytania:
    - pobiera wpisowe 500 zł od graczy z min. 500 zł,
    - jeśli mniej niż 2 takich graczy -> koniec gry,
    - ustawia PHASE='bidding', ROUND_ID++, ROUND_START_TS itp.
    """
    global ROUND_ID, PHASE, ROUND_START_TS, BIDS

    if not QUESTIONS or CURRENT_Q_INDEX < 0 or CURRENT_Q_INDEX >= len(QUESTIONS):
        _end_game_no_more_questions()
        return

    eligible = [p for p in PLAYERS.values() if not p.is_observer and p.money >= 500]

    if len(eligible) < 2:
        _end_game_insufficient_players()
        return

    ROUND_ID += 1
    PHASE = "bidding"
    ROUND_START_TS = time.time()
    BIDS = {}

    # wpisowe 500 zł
    for p in eligible:
        p.money -= 500
        bid = BidInfo(
            player_id=p.id,
            amount=500,
            is_all_in=False,
            ts=time.time(),
        )
        BIDS[p.id] = bid
        CHAT.append(
            ChatMessage(
                player=p.name,
                message=f"{bid.amount} zł (wpisowe do rundy)",
                timestamp=time.time(),
            )
        )

    _recompute_pot()

    q_num = CURRENT_Q_INDEX + 1
    total_q = len(QUESTIONS)
    CHAT.append(
        ChatMessage(
            player="BOT",
            message=(
                f"Rozpoczynamy licytację do pytania {q_num}/{total_q} "
                f"(Zestaw {CURRENT_SET}). Czas: {int(BIDDING_DURATION)} s!"
            ),
            timestamp=time.time(),
        )
    )


def _start_next_question_or_end_game() -> None:
    """
    Przejście do kolejnego pytania lub zakończenie gry, jeśli pytań brak.
    """
    global CURRENT_Q_INDEX, CURRENT_ANSWER_TEXT, CURRENT_ANSWER_PLAYER_ID, ANSWER_SUBMITTED_TS
    CURRENT_ANSWER_TEXT = None
    CURRENT_ANSWER_PLAYER_ID = None
    ANSWER_SUBMITTED_TS = 0.0

    if not QUESTIONS:
        CHAT.append(
            ChatMessage(
                player="BOT",
                message="Brak pytań – gra nie może się rozpocząć.",
                timestamp=time.time(),
            )
        )
        return

    if CURRENT_Q_INDEX + 1 >= len(QUESTIONS):
        _end_game_no_more_questions()
        return

    CURRENT_Q_INDEX += 1
    _start_new_bidding_round()


def _auto_finalize_discussion_if_needed() -> None:
    """
    Po 20 sekundach od udzielenia odpowiedzi ogłoś wynik,
    zaktualizuj kasę/pulę i przejdź do kolejnej rundy.
    """
    global PHASE, POT, CURRENT_ANSWER_TEXT, CURRENT_ANSWER_PLAYER_ID

    if PHASE != "discussion":
        return

    if not CURRENT_ANSWER_TEXT or not CURRENT_ANSWER_PLAYER_ID:
        return

    now = time.time()
    if now - ANSWER_SUBMITTED_TS < 20.0:
        return

    # ocena odpowiedzi
    if 0 <= CURRENT_Q_INDEX < len(QUESTIONS):
        q = QUESTIONS[CURRENT_Q_INDEX]
        correct = q["correct"]
        sim = _similarity(CURRENT_ANSWER_TEXT, correct)
        is_good = sim >= 80
    else:
        correct = "brak danych"
        is_good = False

    winner = PLAYERS.get(CURRENT_ANSWER_PLAYER_ID)

    if is_good and winner:
        winner.money += POT
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=(
                    f"DOBRA odpowiedź! {winner.name} zgarnia z puli {POT} zł. "
                    f"Poprawna odpowiedź: {correct}"
                ),
                timestamp=time.time(),
            )
        )
        POT = 0
    else:
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=(
                    f"ZŁA odpowiedź! Pula {POT} zł przechodzi do kolejnej rundy. "
                    f"Poprawna odpowiedź: {correct}"
                ),
                timestamp=time.time(),
            )
        )

    # przygotuj kolejną rundę
    PHASE = "idle"
    _start_next_question_or_end_game()


def _auto_advance_game_state() -> None:
    """
    Wywoływane przy każdym /state lub /bid:
    - kończy licytację po czasie,
    - kończy dyskusję po 20 s od odpowiedzi,
    - sprząta nieaktywnych graczy.
    """
    _auto_finish_bidding_if_needed()
    _auto_finalize_discussion_if_needed()
    _cleanup_inactive_players()


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
    Jeśli graczy jest więcej niż MAX_ACTIVE_PLAYERS – nowi są obserwatorami.
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

    # komunikat, gdy są już min. 2 gracze
    active_players = [p for p in PLAYERS.values() if not p.is_observer]
    if len(active_players) >= 2:
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=(
                    "Dołączyło co najmniej 2 graczy – ADMIN może wybrać zestaw pytań "
                    "wpisując numer 01–50."
                ),
                timestamp=time.time(),
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

    # Sprzątanie nieaktywnych
    _cleanup_inactive_players()

    is_admin_now = PLAYERS.get(req.player_id, player).is_admin

    return {"status": "ok", "is_admin": is_admin_now}


@app.post("/select_set")
def select_set(req: SelectSetRequest):
    """
    ADMIN wybiera zestaw pytań (1–50).
    Backend ładuje pytania z GitHuba i rozpoczyna pierwszą rundę.
    """
    global QUESTIONS, CURRENT_SET, CURRENT_Q_INDEX, PHASE, POT, BIDS

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nie ma takiego gracza.")
    player = PLAYERS[req.player_id]
    if not player.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Tylko ADMIN może wybierać zestaw pytań.",
        )

    try:
        questions = _load_question_set(req.set_no)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Nie udało się załadować zestawu {req.set_no:02d}: {e}",
        )

    if not questions:
        raise HTTPException(
            status_code=400,
            detail=f"Zestaw {req.set_no:02d} nie zawiera poprawnych pytań.",
        )

    QUESTIONS = questions
    CURRENT_SET = f"{req.set_no:02d}"
    CURRENT_Q_INDEX = -1
    PHASE = "idle"
    POT = 0
    BIDS = {}

    CHAT.append(
        ChatMessage(
            player="BOT",
            message=(
                f"ADMIN {player.name} wybrał zestaw pytań nr {CURRENT_SET}. "
                "Za moment rozpoczniemy pierwszą rundę."
            ),
            timestamp=time.time(),
        )
    )

    _start_next_question_or_end_game()

    return {
        "status": "ok",
        "set": CURRENT_SET,
        "total_questions": len(QUESTIONS),
    }


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

    _auto_advance_game_state()

    if PHASE != "bidding":
        raise HTTPException(
            status_code=400,
            detail="Ta runda nie jest w fazie licytacji.",
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

        # komunikat na czacie: aktualna kwota tego gracza (wpisowe + licytacja)
        total_bid = BIDS[req.player_id].amount
        CHAT.append(
            ChatMessage(
                player=player.name,
                message=f"{total_bid} zł (licytacja)",
                timestamp=time.time(),
            )
        )

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

        total_bid = BIDS[req.player_id].amount
        CHAT.append(
            ChatMessage(
                player=player.name,
                message=f"{total_bid} zł (VA BANQUE!)",
                timestamp=time.time(),
            )
        )

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

    _auto_advance_game_state()
    _finish_bidding(trigger="admin")
    return {
        "status": "ok",
        "phase": PHASE,
        "answering_player_id": ANSWERING_PLAYER_ID,
        "pot": POT,
    }


@app.post("/answer")
def submit_answer(req: AnswerRequest):
    """
    Zwycięzca licytacji przesyła swoją odpowiedź.
    Odpowiedź trafia na czat, BOT zadaje pytanie do innych,
    a po ~20 s automatycznie ogłaszamy werdykt.
    """
    global PHASE, CURRENT_ANSWER_TEXT, CURRENT_ANSWER_PLAYER_ID, ANSWER_SUBMITTED_TS

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nie ma takiego gracza.")

    if PHASE != "answering":
        raise HTTPException(
            status_code=400,
            detail="Nie jest to moment na udzielanie odpowiedzi.",
        )

    if req.player_id != ANSWERING_PLAYER_ID:
        raise HTTPException(
            status_code=403,
            detail="Tylko zwycięzca licytacji może odpowiedzieć.",
        )

    player = PLAYERS[req.player_id]

    CURRENT_ANSWER_TEXT = req.answer
    CURRENT_ANSWER_PLAYER_ID = req.player_id
    ANSWER_SUBMITTED_TS = time.time()
    PHASE = "discussion"

    # odpowiedź gracza na czacie
    CHAT.append(
        ChatMessage(
            player=player.name,
            message=f"ODPOWIEDŹ: {req.answer}",
            timestamp=ANSWER_SUBMITTED_TS,
        )
    )
    # pytanie BOTA do reszty
    CHAT.append(
        ChatMessage(
            player="BOT",
            message="A Wy jak myślicie, mistrzowie? Czy to jest poprawna odpowiedź?",
            timestamp=time.time(),
        )
    )

    return {"status": "ok", "phase": PHASE}


@app.post("/hint")
def buy_hint(req: HintRequest):
    """
    Podpowiedzi dla zwycięzcy licytacji:
    - kind = "abcd" -> pokazuje opcje ABCD
    - kind = "5050" -> usuwa 2 błędne odpowiedzi
    Koszt jest losowy, kwota trafia do puli.
    """
    global POT

    if req.player_id not in PLAYERS:
        raise HTTPException(status_code=404, detail="Nie ma takiego gracza.")

    if PHASE not in ("answering", "discussion"):
        raise HTTPException(
            status_code=400,
            detail="Podpowiedzi można używać tylko podczas odpowiadania na pytanie.",
        )

    if req.player_id != ANSWERING_PLAYER_ID:
        raise HTTPException(
            status_code=403,
            detail="Tylko zwycięzca licytacji może kupić podpowiedź.",
        )

    if not QUESTIONS or not (0 <= CURRENT_Q_INDEX < len(QUESTIONS)):
        raise HTTPException(status_code=400, detail="Brak aktualnego pytania.")

    import random

    player = PLAYERS[req.player_id]
    q = QUESTIONS[CURRENT_Q_INDEX]
    answers = q["answers"]
    correct = q["correct"]

    if req.kind == "abcd":
        cost = random.randint(1000, 3000)
    elif req.kind == "5050":
        cost = random.randint(500, 2500)
    else:
        raise HTTPException(status_code=400, detail="Nieznany rodzaj podpowiedzi.")

    if player.money < cost:
        raise HTTPException(
            status_code=400,
            detail=f"Nie stać Cię na tę podpowiedź (koszt {cost} zł).",
        )

    player.money -= cost
    POT += cost

    if req.kind == "abcd":
        # po prostu wypisz wszystkie odpowiedzi
        opts = ", ".join(answers)
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=(
                    f"{player.name} kupuje podpowiedź ABCD za {cost} zł. "
                    f"Opcje odpowiedzi: {opts}"
                ),
                timestamp=time.time(),
            )
        )
    else:
        wrong = [a for a in answers if _similarity(a, correct) < 90]
        if len(wrong) <= 2:
            removed = wrong
        else:
            removed = random.sample(wrong, 2)
        remaining = [a for a in answers if a not in removed]
        CHAT.append(
            ChatMessage(
                player="BOT",
                message=(
                    f"{player.name} kupuje podpowiedź 50/50 za {cost} zł. "
                    f"Usuwam dwie błędne odpowiedzi: {', '.join(removed)}. "
                    f"Pozostają: {', '.join(remaining)}."
                ),
                timestamp=time.time(),
            )
        )

    return {"status": "ok", "pot": POT, "kind": req.kind}


@app.post("/next_round")
def next_round():
    """
    Ręczne przejście do kolejnego pytania (np. awaryjnie).
    Normalnie gra sama wywołuje tę logikę po ocenie odpowiedzi.
    """
    _start_next_question_or_end_game()
    return {"status": "ok", "round_id": ROUND_ID}


@app.get("/state", response_model=StateResponse)
def get_state():
    """
    Zwraca aktualny stan:
    - runda, faza, czas do końca licytacji
    - graczy wraz z ich stawkami i kasą
    - aktualne pytanie
    - czat (ostatnie ~30 wiadomości)
    """
    _auto_advance_game_state()

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

    current_q_text = None
    if 0 <= CURRENT_Q_INDEX < len(QUESTIONS):
        current_q_text = QUESTIONS[CURRENT_Q_INDEX]["question"]

    return StateResponse(
        round_id=ROUND_ID,
        phase=PHASE,
        pot=POT,
        time_left=_time_left(),
        answering_player_id=ANSWERING_PLAYER_ID,
        current_set=CURRENT_SET,
        current_question_index=CURRENT_Q_INDEX,
        current_question_text=current_q_text,
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
