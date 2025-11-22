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

app = FastAPI(title="Awantura o Kasę – Multiplayer Backend (Fixed)")

origins = ["*"]  # Dla uproszczenia developmentu

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
    # Flaga dla frontendu, czy w tej rundzie kupiono już ABCD
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

# --- STAN SERWERA ---

PLAYERS: Dict[str, Player] = {}
BIDS: Dict[str, BidInfo] = {}
CHAT: List[ChatMessage] = []

ROUND_ID: int = 0
PHASE: str = "idle"  # idle, bidding, answering, discussion
ROUND_START_TS: float = time.time()
BIDDING_DURATION: float = 20.0
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

# Flaga rundy: czy kupiono ABCD (żeby odblokować 50/50)
ROUND_ABCD_BOUGHT: bool = False
# Przechowujemy usunięte odpowiedzi dla spójności 50/50
ROUND_REMOVED_ANSWERS: List[str] = []

# --- FUNKCJE POMOCNICZE ---

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

def _recompute_pot() -> None:
    # Pula to suma wszystkich licytacji w tej rundzie (plus przeniesiona pula, plus podpowiedzi)
    # Tutaj POT przechowuje "carryover + hints", a licytacje są w BIDS.
    # Frontend wyświetla sumę. Dla backendu ważne, żeby przy finale dodać BIDS do POT.
    pass

def _time_left() -> float:
    if PHASE != "bidding": return 0.0
    now = time.time()
    left = BIDDING_DURATION - (now - ROUND_START_TS)
    return max(0.0, left)

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
        
        # Proste regexy do wyciągnięcia
        correct_m = re.search(r"(praw\w*\s*odpow\w*|correct)\s*[:=]\s*(.+)", block, re.IGNORECASE)
        if not correct_m: continue
        correct = correct_m.group(2).strip()

        # Szukamy A=..., B=...
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
    
    # Jeśli brak admina, mianuj nowego
    if PLAYERS and not any(p.is_admin for p in PLAYERS.values()):
        first = next(iter(PLAYERS))
        PLAYERS[first].is_admin = True
        CHAT.append(ChatMessage(player="BOT", message=f"{PLAYERS[first].name} jest nowym ADMINEM.", timestamp=now))

def _finish_bidding(trigger: str) -> None:
    global PHASE, ANSWERING_PLAYER_ID, ROUND_ABCD_BOUGHT, ROUND_REMOVED_ANSWERS
    
    # Reset flag podpowiedzi na nową fazę odpowiedzi
    ROUND_ABCD_BOUGHT = False
    ROUND_REMOVED_ANSWERS = []

    if not BIDS:
        ANSWERING_PLAYER_ID = None
        PHASE = "answering" # lub przejdz dalej, ale zostawmy answering zeby pokazac brak
        CHAT.append(ChatMessage(player="BOT", message="Brak ofert. Nikt nie odpowiada.", timestamp=time.time()))
        return

    # Wybierz najwyższą ofertę
    best = None
    for bid in BIDS.values():
        if best is None: best = bid
        else:
            if bid.amount > best.amount: best = bid
            elif bid.amount == best.amount and bid.ts < best.ts: best = bid
    
    ANSWERING_PLAYER_ID = best.player_id if best else None
    PHASE = "answering"

    if ANSWERING_PLAYER_ID and ANSWERING_PLAYER_ID in PLAYERS:
        winner = PLAYERS[ANSWERING_PLAYER_ID]
        CHAT.append(ChatMessage(player="BOT", message=f"Licytację wygrywa {winner.name} ({best.amount} zł).", timestamp=time.time()))

    # Dodaj licytacje do puli globalnej (oprócz wpisowego, które już pobrano)
    # Uwaga: wpisowe 500 było pobierane przy starcie rundy. Tutaj dodajemy to co w licytacji.
    # W uproszczeniu: BIDS zawiera CAŁĄ kwotę (wpisowe + podbicie).
    # Ale wpisowe odejmowaliśmy z konta gracza na początku. 
    # Żeby nie dublować: w POT trzymamy "martwą kasę". 
    # Tutaj przenosimy sumę licytacji z BIDS do POT.
    global POT
    round_bids_total = sum(b.amount for b in BIDS.values())
    POT += round_bids_total
    
    # Wyczyść BIDS, bo kasa już w POT
    # BIDS = {} # Nie czyścimy, bo chcemy wyświetlać w stanie. Wyczyścimy przy starcie nowej.

    if 0 <= CURRENT_Q_INDEX < len(QUESTIONS):
        CHAT.append(ChatMessage(player="BOT", message=f"PYTANIE: {QUESTIONS[CURRENT_Q_INDEX]['question']}", timestamp=time.time()))

def _reset_game():
    """Resetuje grę do stanu początkowego (wybór zestawu), ale zostawia graczy."""
    global POT, ROUND_ID, PHASE, QUESTIONS, CURRENT_SET, CURRENT_Q_INDEX, BIDS
    
    POT = 0
    ROUND_ID = 0
    PHASE = "idle"
    QUESTIONS = []
    CURRENT_SET = None
    CURRENT_Q_INDEX = -1
    BIDS = {}
    
    # Reset kasy graczy
    for p in PLAYERS.values():
        p.money = 10000
        p.is_observer = False # Przywracamy obserwatorów do gry
    
    CHAT.append(ChatMessage(player="BOT", message="--- NOWA GRA ---", timestamp=time.time()))
    CHAT.append(ChatMessage(player="BOT", message="Konta zresetowane do 10 000 zł. Admin, wybierz zestaw!", timestamp=time.time()))

def _check_game_over_or_next_round():
    """Sprawdza warunki końca gry (brak kasy u graczy)."""
    global PHASE, POT
    
    # Gracze zdolni do gry (mają >= 500 zł)
    active_capable = [p for p in PLAYERS.values() if not p.is_observer and p.money >= 500]
    
    # Jeśli zostało mniej niż 2 graczy z kasą:
    if len(active_capable) < 2:
        PHASE = "finished"
        
        # Wyznacz zwycięzcę (ten co został, lub ten co ma najwięcej jeśli wszyscy odpadli)
        all_active = [p for p in PLAYERS.values() if not p.is_observer]
        if not all_active: all_active = list(PLAYERS.values())
        
        if active_capable:
            winner = active_capable[0]
            winner.money += POT # Zgarnia pulę
            msg = f"Grę wygrywa {winner.name} z kwotą na koncie: {winner.money}zł!"
        elif all_active:
            winner = max(all_active, key=lambda p: p.money)
            winner.money += POT
            msg = f"Koniec gry! Wygrywa {winner.name} z kwotą: {winner.money}zł."
        else:
            msg = "Gra zakończona. Brak graczy."
            
        CHAT.append(ChatMessage(player="BOT", message=msg, timestamp=time.time()))
        POT = 0
        
        # Reset po 5 sekundach (symulacja, w praktyce robimy to od razu lub admin klika)
        # Tu zrobimy od razu reset stanu, żeby admin mógł wybrać zestaw.
        _reset_game()
        return

    # Jeśli gra toczy się dalej:
    global CURRENT_Q_INDEX
    if CURRENT_Q_INDEX + 1 >= len(QUESTIONS):
        CHAT.append(ChatMessage(player="BOT", message="Koniec pytań w zestawie!", timestamp=time.time()))
        # Też resetujemy, bo nie ma pytań
        # Ewentualnie wyznaczamy zwycięzcę z kasy
        winner = max(active_capable, key=lambda p: p.money)
        CHAT.append(ChatMessage(player="BOT", message=f"Koniec zestawu. Wygrywa {winner.name} ({winner.money} zł).", timestamp=time.time()))
        _reset_game()
        return

    CURRENT_Q_INDEX += 1
    _start_new_bidding_round()

def _start_new_bidding_round():
    global ROUND_ID, PHASE, ROUND_START_TS, BIDS, POT, PLAYERS
    
    # Sprawdzenie kto ma < 500 i zmiana na obserwatora
    for p in PLAYERS.values():
        if not p.is_observer and p.money < 500:
            p.is_observer = True
            CHAT.append(ChatMessage(player="BOT", message=f"Gracz {p.name} ma mniej niż 500zł i zostaje obserwatorem.", timestamp=time.time()))
    
    # Sprawdź czy jest sens grać
    active = [p for p in PLAYERS.values() if not p.is_observer]
    if len(active) < 2:
        _check_game_over_or_next_round() # To obsłuży koniec gry
        return

    ROUND_ID += 1
    PHASE = "bidding"
    ROUND_START_TS = time.time()
    BIDS = {}
    
    # Pobierz wpisowe
    for p in active:
        p.money -= 500
        # Wpisowe trafia do BIDS jako startowa oferta (dla uproszczenia wyświetlania)
        # Ale technicznie w naszej logice wpisowe = POT, licytacja = POT.
        # Zróbmy tak: Wpisowe od razu do POT. Licytacja w BIDS, potem do POT.
        # Żeby jednak BIDS pokazywało "ile dałem w tej rundzie", dodajemy tu wpis.
        BIDS[p.id] = BidInfo(player_id=p.id, amount=500, is_all_in=False, ts=time.time())
    
    CHAT.append(ChatMessage(player="BOT", message=f"Runda {CURRENT_Q_INDEX+1}/{len(QUESTIONS)}. Wpisowe pobrane. Licytacja start!", timestamp=time.time()))

def _auto_finalize_discussion_if_needed():
    global PHASE, POT, CURRENT_ANSWER_TEXT, CURRENT_ANSWER_PLAYER_ID
    if PHASE != "discussion": return
    if not CURRENT_ANSWER_TEXT: return
    
    if time.time() - ANSWER_SUBMITTED_TS < 15.0: # 15 sekund na dyskusję
        return

    q = QUESTIONS[CURRENT_Q_INDEX]
    correct = q["correct"]
    sim = _similarity(CURRENT_ANSWER_TEXT, correct)
    is_good = sim >= 75 # Tolerancja
    
    winner = PLAYERS.get(CURRENT_ANSWER_PLAYER_ID)
    if winner:
        if is_good:
            winner.money += POT
            msg = f"Poprawna odpowiedź: {correct}. Gracz {winner.name} wygrywa {POT} zł!"
            POT = 0 # Pula wyczyszczona
        else:
            msg = f"Niestety, to błędna odpowiedź. Poprawna to: {correct}. Pula {POT} zł przechodzi do następnej rundy."
            # POT zostaje bez zmian (carryover)
        
        CHAT.append(ChatMessage(player="BOT", message=msg, timestamp=time.time()))
    
    PHASE = "idle" # Stan przejściowy
    _check_game_over_or_next_round()

def _auto_advance_game_state():
    if PHASE == "bidding" and _time_left() <= 0:
        _finish_bidding("timer")
    _auto_finalize_discussion_if_needed()
    _cleanup_inactive_players()

# --- ENDPOINTY ---

@app.get("/state", response_model=StateResponse)
def get_state():
    _auto_advance_game_state()
    
    # Sumowanie puli do wyświetlenia (POT + obecne licytacje)
    display_pot = POT
    if PHASE == "bidding":
        # W fazie licytacji BIDS zawiera (wpisowe 500 + podbicia)
        # Wpisowe gracze już wpłacili (odjęto z money), ale my dodaliśmy to do BIDS a nie POT (jeszcze).
        # Więc display_pot = POT(stare) + Suma(BIDS)
        # Ale uwaga: _start_new_bidding_round tworzy BIDS z 500.
        # Więc po prostu sumujemy BIDS.
        current_bids_sum = sum(b.amount for b in BIDS.values())
        # display_pot += current_bids_sum - (len(BIDS) * 500) # Jeśli chcemy być super precyzyjni co jest w POT a co w BIDS
        # Uprośćmy: Wyświetlamy POT (stare) + to co leży na stole w licytacji
        # Ponieważ w _start_new_bidding nie dodajemy do POT, tylko do BIDS.
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
    
    # Nowa gra - upewnijmy się, że kasa jest zresetowana (jeśli admin klika select_set w trakcie gry, to hard reset)
    # Ale funkcja _reset_game jest wołana po game over. Tutaj zakładamy start.
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
    if req.kind == "allin" and p.money <= 0: raise HTTPException(400, "Brak kasy na VA BANQUE.")
    
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
    # Pozwalamy pasować każdemu, nie tylko adminowi? 
    # Oryginał pozwalał adminowi kończyć CAŁĄ fazę.
    # Użytkownik chce przycisk "Pasuję" dla gracza.
    # W tej logice "Pasuję" oznacza "nie licytuję więcej".
    # Flet wysyła to tylko jako komunikat. 
    # Ale jeśli admin klika "Zakończ licytację" to kończy fazę.
    if p.is_admin:
        _finish_bidding("admin")
    else:
        CHAT.append(ChatMessage(player=p.name, message="Pasuję.", timestamp=time.time()))
    return {"status": "ok"}

@app.post("/answer")
def answer(req: AnswerRequest):
    global PHASE, CURRENT_ANSWER_TEXT, CURRENT_ANSWER_PLAYER_ID, ANSWER_SUBMITTED_TS
    if PHASE != "answering" or req.player_id != ANSWERING_PLAYER_ID:
        raise HTTPException(400, "Nie twoja kolej.")
    
    CURRENT_ANSWER_TEXT = req.answer
    CURRENT_ANSWER_PLAYER_ID = req.player_id
    ANSWER_SUBMITTED_TS = time.time()
    PHASE = "discussion"
    
    CHAT.append(ChatMessage(player=PLAYERS[req.player_id].name, message=f"ODPOWIEDŹ: {req.answer}", timestamp=time.time()))
    CHAT.append(ChatMessage(player="BOT", message="A wy jak myślicie?", timestamp=time.time()))
    return {"status": "ok"}

@app.post("/hint")
def hint(req: HintRequest):
    global POT, ROUND_ABCD_BOUGHT, ROUND_REMOVED_ANSWERS
    if PHASE != "answering" or req.player_id != ANSWERING_PLAYER_ID:
        raise HTTPException(400, "Nie możesz.")
    
    p = PLAYERS[req.player_id]
    q = QUESTIONS[CURRENT_Q_INDEX]
    
    if req.kind == "abcd":
        cost = 2500 # Stała cena lub losowa, w promptcie była mowa o losowej 1000-3000
        cost = random.randint(1000, 3000)
    elif req.kind == "5050":
        if not ROUND_ABCD_BOUGHT:
            raise HTTPException(400, "Musisz najpierw kupić ABCD!")
        cost = random.randint(500, 2500)
    else: return
    
    if p.money < cost: raise HTTPException(400, "Brak kasy.")
    
    p.money -= cost
    POT += cost # Kasa za podpowiedź idzie do puli
    
    if req.kind == "abcd":
        ROUND_ABCD_BOUGHT = True
        # Formatowanie A. ... B. ...
        opts = q["answers"]
        # Zakładamy kolejność A, B, C, D jak w pliku
        msg_text = f"A. {opts[0]}   B. {opts[1]}   C. {opts[2]}   D. {opts[3]}"
        CHAT.append(ChatMessage(player="BOT", message=f"Podpowiedź ABCD ({cost} zł): {msg_text}", timestamp=time.time()))
        
    elif req.kind == "5050":
        opts = q["answers"]
        correct = q["correct"]
        # Znajdź błędne
        wrongs = [o for o in opts if _similarity(o, correct) < 90]
        # Usuń 2 błędne
        if len(wrongs) >= 2:
            to_remove = random.sample(wrongs, 2)
            ROUND_REMOVED_ANSWERS = to_remove
            CHAT.append(ChatMessage(player="BOT", message=f"Podpowiedź 50/50 ({cost} zł): Odrzucam: {to_remove[0]} oraz {to_remove[1]}", timestamp=time.time()))
    
    return {"status": "ok"}

@app.post("/chat")
def chat(req: ChatRequest):
    CHAT.append(ChatMessage(player=req.player, message=req.message, timestamp=time.time()))
    return {"status": "ok"}

# Endpoints required by frontend logic but redundant logic wise
@app.post("/next_round") 
def nr(): return {"status": "ok"}
