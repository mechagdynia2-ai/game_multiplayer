from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import time
import uuid
import re
import difflib
import random
from urllib.request import urlopen

app = FastAPI(title="Awantura o Kasę – Multiplayer Backend (Final Fix)")

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GITHUB_QUESTIONS_BASE = "https://raw.githubusercontent.com/mechagdynia2-ai/game/main/assets/"

# --- MODELE ---

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
    kind: str 

class FinishBiddingRequest(BaseModel):
    player_id: str

class ChatRequest(BaseModel):
    player: str
    message: str

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
    abcd_bought: bool

class SelectSetRequest(BaseModel):
    player_id: str
    set_no: int

class AnswerRequest(BaseModel):
    player_id: str
    answer: str

class HintRequest(BaseModel):
    player_id: str
    kind: str

# --- STAN ---

PLAYERS: Dict[str, Player] = {}
BIDS: Dict[str, BidInfo] = {}
CHAT: List[ChatMessage] = []

ROUND_ID: int = 0
PHASE: str = "idle"  
ROUND_START_TS: float = time.time()

ANSWER_DEADLINE: float = 0.0 
DISCUSSION_DEADLINE: float = 0.0

POT: int = 0
ANSWERING_PLAYER_ID: Optional[str] = None

HEARTBEAT_TIMEOUT: float = 60.0
MAX_ACTIVE_PLAYERS: int = 20

QUESTIONS: List[Dict] = []
CURRENT_SET: Optional[str] = None
CURRENT_Q_INDEX: int = -1

CURRENT_ANSWER_TEXT: Optional[str] = None
CURRENT_ANSWER_PLAYER_ID: Optional[str] = None
ANSWER_SUBMITTED_TS: float = 0.0

ROUND_ABCD_BOUGHT: bool = False
ROUND_REMOVED_ANSWERS: List[str] = []

# --- HELPERS ---

def _normalize_answer(text: str) -> str:
    text = str(text).lower().strip()
    repl = {"ó": "o", "ł": "l", "ż": "z", "ź": "z", "ć": "c", "ń": "n", "ś": "s", "ą": "a", "ę": "e", "ü": "u"}
    for c, r in repl.items():
        text = text.replace(c, r)
    text = text.replace("u", "o")
    return "".join(text.split())

def _similarity(a: str, b: str) -> int:
    na = _normalize_answer(a)
    nb = _normalize_answer(b)
    if not na and not nb: return 100
    return int(difflib.SequenceMatcher(None, na, nb).ratio() * 100)

def _time_left() -> float:
    now = time.time()
    if PHASE == "bidding":
        return max(0.0, 20.0 - (now - ROUND_START_TS))
    elif PHASE == "answering":
        return max(0.0, ANSWER_DEADLINE - now)
    elif PHASE == "discussion":
        return max(0.0, DISCUSSION_DEADLINE - now)
    return 0.0

def _load_question_set(set_no: int) -> List[Dict]:
    filename = f"{set_no:02d}.txt"
    url = GITHUB_QUESTIONS_BASE + filename
    try:
        with urlopen(url) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return []

    parsed: List[Dict] = []
    blocks = re.split(r"\n(?=\d{1,3}\.)", content)
    for block in blocks:
        block = block.strip()
        if not block: continue
        q_match = re.match(r"^\d{1,3}\.\s*(.+)", block)
        if not q_match: continue
        question = q_match.group(1).strip()
        
        correct_m = re.search(r"(praw\w*\s*odpow\w*|correct)\s*[:=]\s*(.+)", block, re.IGNORECASE)
        if not correct_m: continue
        correct = correct_m.group(2).strip()

        answers = []
        for l in ["A", "B", "C", "D"]:
            m = re.search(rf"\b{l}\s*=\s*(.+?)(?:,|\n|$)", block, re.IGNORECASE)
            if m: answers.append(m.group(1).strip())
        
        if len(answers) == 4:
            parsed.append({"question": question, "correct": correct, "answers": answers})
    return parsed

def _cleanup_inactive_players() -> None:
    now = time.time()
    removed = []
    for pid, p in list(PLAYERS.items()):
        if now - p.last_heartbeat > HEARTBEAT_TIMEOUT:
            removed.append(pid)
    for pid in removed:
        pl = PLAYERS.pop(pid, None)
        if pl:
            BIDS.pop(pid, None)
            CHAT.append(ChatMessage(player="BOT", message=f"Gracz {pl.name} rozłączony.", timestamp=now))
    
    if PLAYERS and not any(p.is_admin for p in PLAYERS.values()):
        first = next(iter(PLAYERS))
        PLAYERS[first].is_admin = True
        CHAT.append(ChatMessage(player="BOT", message=f"{PLAYERS[first].name} jest nowym ADMINEM.", timestamp=now))

def _finish_bidding(trigger: str) -> None:
    global PHASE, ANSWERING_PLAYER_ID, ROUND_ABCD_BOUGHT, ROUND_REMOVED_ANSWERS, ANSWER_DEADLINE
    
    ROUND_ABCD_BOUGHT = False
    ROUND_REMOVED_ANSWERS = []

    if not BIDS:
        ANSWERING_PLAYER_ID = None
        PHASE = "answering" 
        ANSWER_DEADLINE = time.time() + 5.0 
        CHAT.append(ChatMessage(player="BOT", message="Brak ofert.", timestamp=time.time()))
        return

    best = None
    for bid in BIDS.values():
        if best is None: best = bid
        else:
            if bid.amount > best.amount: best = bid
            elif bid.amount == best.amount and bid.ts < best.ts: best = bid
    
    ANSWERING_PLAYER_ID = best.player_id if best else None
    PHASE = "answering"
    ANSWER_DEADLINE = time.time() + 60.0

    if ANSWERING_PLAYER_ID and ANSWERING_PLAYER_ID in PLAYERS:
        winner = PLAYERS[ANSWERING_PLAYER_ID]
        CHAT.append(ChatMessage(player="BOT", message=f"Licytację wygrywa {winner.name} ({best.amount} zł).", timestamp=time.time()))

    global POT
    POT += sum(b.amount for b in BIDS.values())
    
    if 0 <= CURRENT_Q_INDEX < len(QUESTIONS):
        # Dodajemy drobne opóźnienie timestampu (0.01s), aby frontend na pewno odróżnił to od poprzedniej wiadomości
        CHAT.append(ChatMessage(player="BOT", message=f"PYTANIE: {QUESTIONS[CURRENT_Q_INDEX]['question']}", timestamp=time.time() + 0.01))

def _reset_game():
    global POT, ROUND_ID, PHASE, QUESTIONS, CURRENT_SET, CURRENT_Q_INDEX, BIDS
    POT = 0
    ROUND_ID = 0
    PHASE = "idle"
    QUESTIONS = []
    CURRENT_SET = None
    CURRENT_Q_INDEX = -1
    BIDS = {}
    for p in PLAYERS.values():
        p.money = 10000
        p.is_observer = False
    
    CHAT.append(ChatMessage(player="BOT", message="--- NOWA GRA ---", timestamp=time.time()))
    CHAT.append(ChatMessage(player="BOT", message="Konta zresetowane do 10 000 zł. Admin, wybierz zestaw!", timestamp=time.time()))

def _check_game_over_or_next_round():
    global PHASE, POT, CURRENT_Q_INDEX
    
    active_capable = [p for p in PLAYERS.values() if not p.is_observer and p.money >= 500]
    
    if len(active_capable) < 2:
        PHASE = "finished"
        all_active = [p for p in PLAYERS.values() if not p.is_observer]
        if not all_active: all_active = list(PLAYERS.values())
        
        if active_capable:
            winner = active_capable[0]
            winner.money += POT
            msg = f"Grę wygrywa {winner.name} z kwotą na koncie: {winner.money}zł!"
        elif all_active:
            winner = max(all_active, key=lambda p: p.money)
            winner.money += POT
            msg = f"Koniec gry! Wygrywa {winner.name} z kwotą: {winner.money}zł."
        else:
            msg = "Gra zakończona. Brak graczy."
            
        CHAT.append(ChatMessage(player="BOT", message=msg, timestamp=time.time()))
        POT = 0
        _reset_game()
        return

    if CURRENT_Q_INDEX + 1 >= len(QUESTIONS):
        CHAT.append(ChatMessage(player="BOT", message="Koniec pytań w zestawie!", timestamp=time.time()))
        winner = max(active_capable, key=lambda p: p.money)
        CHAT.append(ChatMessage(player="BOT", message=f"Koniec zestawu. Wygrywa {winner.name} ({winner.money} zł).", timestamp=time.time()))
        _reset_game()
        return

    CURRENT_Q_INDEX += 1
    _start_new_bidding_round()

def _start_new_bidding_round():
    global ROUND_ID, PHASE, ROUND_START_TS, BIDS, POT, PLAYERS
    
    for p in PLAYERS.values():
        if not p.is_observer and p.money < 500:
            p.is_observer = True
            CHAT.append(ChatMessage(player="BOT", message=f"Gracz {p.name} ma mniej niż 500zł i zostaje obserwatorem.", timestamp=time.time()))
    
    active = [p for p in PLAYERS.values() if not p.is_observer]
    if len(active) < 2:
        _check_game_over_or_next_round()
        return

    ROUND_ID += 1
    PHASE = "bidding"
    ROUND_START_TS = time.time()
    BIDS = {}
    
    for p in active:
        p.money -= 500
        BIDS[p.id] = BidInfo(player_id=p.id, amount=500, is_all_in=False, ts=time.time())
    
    CHAT.append(ChatMessage(player="BOT", message=f"Runda {CURRENT_Q_INDEX+1}/{len(QUESTIONS)}. Wpisowe pobrane. Licytacja start!", timestamp=time.time()))

def _auto_finalize_discussion_if_needed():
    global PHASE, POT, CURRENT_ANSWER_TEXT, CURRENT_ANSWER_PLAYER_ID
    if PHASE != "discussion": return
    
    if _time_left() > 0: return

    q = QUESTIONS[CURRENT_Q_INDEX]
    correct = q["correct"]
    
    if not CURRENT_ANSWER_TEXT:
        msg = f"Brak odpowiedzi! Poprawna to: {correct}. Pula {POT} zł przechodzi dalej."
    else:
        sim = _similarity(CURRENT_ANSWER_TEXT, correct)
        is_good = sim >= 75
        winner = PLAYERS.get(CURRENT_ANSWER_PLAYER_ID)
        if winner and is_good:
            winner.money += POT
            msg = f"Poprawna odpowiedź! {winner.name} wygrywa {POT} zł! ({correct})"
            POT = 0
        else:
            msg = f"Zła odpowiedź! Poprawna to: {correct}. Pula przechodzi dalej."
            
    CHAT.append(ChatMessage(player="BOT", message=msg, timestamp=time.time()))
    PHASE = "idle"
    _check_game_over_or_next_round()

def _handle_answering_timeout():
    global PHASE, ANSWER_SUBMITTED_TS, DISCUSSION_DEADLINE, CURRENT_ANSWER_TEXT
    if PHASE == "answering" and _time_left() <= 0:
        PHASE = "discussion"
        CURRENT_ANSWER_TEXT = "" 
        DISCUSSION_DEADLINE = time.time() + 20.0
        CHAT.append(ChatMessage(player="BOT", message="Czas minął! Brak odpowiedzi. 20s na dyskusję.", timestamp=time.time()))

def _auto_advance_game_state():
    if PHASE == "bidding" and _time_left() <= 0:
        _finish_bidding("timer")
    if PHASE == "answering":
        _handle_answering_timeout()
    if PHASE == "discussion":
        _auto_finalize_discussion_if_needed()
    _cleanup_inactive_players()

# --- ENDPOINTY ---

@app.get("/state", response_model=StateResponse)
def get_state():
    _auto_advance_game_state()
    
    display_pot = POT
    if PHASE == "bidding":
        display_pot += sum(b.amount for b in BIDS.values())
        
    p_states = []
    for pid, p in PLAYERS.items():
        bid = BIDS.get(pid)
        p_states.append(PlayerState(
            id=p.id, name=p.name, money=p.money,
            bid=bid.amount if bid else 0, is_all_in=bid.is_all_in if bid else False,
            is_admin=p.is_admin
        ))
    
    q_text = QUESTIONS[CURRENT_Q_INDEX]["question"] if 0 <= CURRENT_Q_INDEX < len(QUESTIONS) else None

    return StateResponse(
        round_id=ROUND_ID, phase=PHASE, pot=display_pot, time_left=_time_left(),
        answering_player_id=ANSWERING_PLAYER_ID, current_set=CURRENT_SET,
        current_question_index=CURRENT_Q_INDEX, current_question_text=q_text,
        players=p_states, chat=CHAT[-30:],
        abcd_bought=ROUND_ABCD_BOUGHT
    )

@app.post("/register", response_model=Player)
def register(req: RegisterRequest):
    pid = str(uuid.uuid4())
    is_admin = (len(PLAYERS) == 0)
    p = Player(id=pid, name=req.name, is_admin=is_admin, last_heartbeat=time.time())
    PLAYERS[pid] = p
    CHAT.append(ChatMessage(player="BOT", message=f"{p.name} dołączył.", timestamp=time.time()))
    return p

@app.post("/heartbeat")
def heartbeat(req: HeartbeatRequest):
    if req.player_id in PLAYERS:
        PLAYERS[req.player_id].last_heartbeat = time.time()
    return {"status": "ok"}

@app.post("/select_set")
def select_set(req: SelectSetRequest):
    global QUESTIONS, CURRENT_SET, CURRENT_Q_INDEX, PHASE, POT, BIDS, PLAYERS
    p = PLAYERS.get(req.player_id)
    if not p or not p.is_admin: raise HTTPException(403, "Tylko admin.")
    
    qs = _load_question_set(req.set_no)
    if not qs: raise HTTPException(400, "Błąd zestawu.")
    
    QUESTIONS = qs
    CURRENT_SET = f"{req.set_no}"
    CURRENT_Q_INDEX = -1
    PHASE = "idle"
    POT = 0
    BIDS = {}
    
    CHAT.append(ChatMessage(player="BOT", message=f"Wybrano zestaw {req.set_no}. Start!", timestamp=time.time()))
    _start_new_bidding_round()
    return {"status": "ok"}

@app.post("/bid")
def bid(req: BidRequest):
    global POT
    p = PLAYERS.get(req.player_id)
    if not p or PHASE != "bidding": raise HTTPException(400, "Błąd licytacji.")
    
    cost = 100 if req.kind == "normal" else p.money
    if p.money < cost: raise HTTPException(400, "Brak kasy.")
    
    p.money -= cost
    if req.player_id not in BIDS:
        BIDS[req.player_id] = BidInfo(player_id=p.id, amount=0, is_all_in=False, ts=time.time())
    
    b = BIDS[req.player_id]
    b.amount += cost
    b.ts = time.time()
    if req.kind == "allin": b.is_all_in = True
    
    msg = f"{p.name} podbija o {cost}." if req.kind == "normal" else f"{p.name} VA BANQUE!"
    CHAT.append(ChatMessage(player="BOT", message=msg, timestamp=time.time()))
    
    if req.kind == "allin": _finish_bidding("allin")
    return {"status": "ok"}

@app.post("/finish_bidding")
def finish_bidding_endpoint(req: FinishBiddingRequest):
    p = PLAYERS.get(req.player_id)
    if not p: raise HTTPException(404)
    if p.is_admin:
        _finish_bidding("admin")
    else:
        CHAT.append(ChatMessage(player=p.name, message="Pasuję.", timestamp=time.time()))
    return {"status": "ok"}

@app.post("/answer")
def answer(req: AnswerRequest):
    global PHASE, CURRENT_ANSWER_TEXT, CURRENT_ANSWER_PLAYER_ID, ANSWER_SUBMITTED_TS, DISCUSSION_DEADLINE
    if PHASE != "answering" or req.player_id != ANSWERING_PLAYER_ID:
        raise HTTPException(400, "Nie twoja kolej.")
    
    CURRENT_ANSWER_TEXT = req.answer
    CURRENT_ANSWER_PLAYER_ID = req.player_id
    ANSWER_SUBMITTED_TS = time.time()
    
    PHASE = "discussion"
    DISCUSSION_DEADLINE = time.time() + 20.0
    
    CHAT.append(ChatMessage(player=PLAYERS[req.player_id].name, message=f"ODPOWIEDŹ: {req.answer}", timestamp=time.time()))
    CHAT.append(ChatMessage(player="BOT", message="A wy jak myślicie? (20s na dyskusję)", timestamp=time.time()))
    return {"status": "ok"}

@app.post("/hint")
def hint(req: HintRequest):
    global POT, ROUND_ABCD_BOUGHT, ROUND_REMOVED_ANSWERS, ANSWER_DEADLINE
    if PHASE != "answering" or req.player_id != ANSWERING_PLAYER_ID:
        raise HTTPException(400, "Nie możesz.")
    
    p = PLAYERS[req.player_id]
    q = QUESTIONS[CURRENT_Q_INDEX]
    
    if req.kind == "abcd":
        cost = random.randint(1000, 3000)
    elif req.kind == "5050":
        if not ROUND_ABCD_BOUGHT: raise HTTPException(400, "Najpierw ABCD.")
        cost = random.randint(500, 2500)
    else: return
    
    if p.money < cost: raise HTTPException(400, "Brak kasy.")
    
    p.money -= cost
    POT += cost
    ANSWER_DEADLINE += 30.0
    
    if req.kind == "abcd":
        ROUND_ABCD_BOUGHT = True
        opts = q["answers"]
        msg_text = f"A. {opts[0]}   B. {opts[1]}   C. {opts[2]}   D. {opts[3]}"
        CHAT.append(ChatMessage(player="BOT", message=f"ABCD ({cost} zł): {msg_text} (+30s)", timestamp=time.time()))
        
    elif req.kind == "5050":
        opts = q["answers"]
        correct = q["correct"]
        wrongs = [o for o in opts if _similarity(o, correct) < 90]
        if len(wrongs) >= 2:
            to_remove = random.sample(wrongs, 2)
            CHAT.append(ChatMessage(player="BOT", message=f"50/50 ({cost} zł): Odrzucam: {to_remove[0]} oraz {to_remove[1]} (+30s)", timestamp=time.time()))
    
    return {"status": "ok"}

@app.post("/chat")
def chat(req: ChatRequest):
    CHAT.append(ChatMessage(player=req.player, message=req.message, timestamp=time.time()))
    return {"status": "ok"}

@app.post("/next_round") 
def nr(): return {"status": "ok"}
