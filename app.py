import random
from flask import Flask, render_template
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'bridge-master-secret-999'
socketio = SocketIO(app, cors_allowed_origins="*")

SUITS = ['♣', '♦', '♥', '♠']
STRAINS = ['♣', '♦', '♥', '♠', 'NT']
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
HCP = {'A': 4, 'K': 3, 'Q': 2, 'J': 1}
SEATS = ['North', 'East', 'South', 'West']
PARTNERS = {'North': 'South', 'South': 'North', 'East': 'West', 'West': 'East'}
LHO = {'North': 'East', 'East': 'South', 'South': 'West', 'West': 'North'}

class BridgeGame:
    def __init__(self):
        self.players = {'South': None, 'North': None}
        self.dealer_idx = 2  # South deals initially
        self.reset_game(next_dealer=False)

    def reset_game(self, next_dealer=True):
        if next_dealer:
            self.dealer_idx = (self.dealer_idx + 1) % 4

        deck = [{'suit': s, 'rank': r, 'val': RANKS.index(r)} for s in SUITS for r in RANKS]
        random.shuffle(deck)

        self.hands = {
            'North': sorted(deck[0:13], key=lambda c: (SUITS.index(c['suit']), c['val'])),
            'East': sorted(deck[13:26], key=lambda c: (SUITS.index(c['suit']), c['val'])),
            'South': sorted(deck[26:39], key=lambda c: (SUITS.index(c['suit']), c['val'])),
            'West': sorted(deck[39:52], key=lambda c: (SUITS.index(c['suit']), c['val']))
        }

        self.phase = 'BIDDING'  # 'BIDDING', 'PLAY', 'HAND_OVER', 'PASSED_OUT'
        self.turn_idx = self.dealer_idx
        self.bids = []
        self.highest_bid = None      # e.g., {'level': 1, 'strain': '♠', 'seat': 'South', 'rank': 3}
        self.consecutive_passes = 0
        
        # Play State
        self.contract = None         # {'level': 4, 'strain': '♠', 'declarer': 'North', 'dummy': 'South', 'target': 10}
        self.dummy_revealed = False
        self.current_trick = []
        self.tricks_won = {'NS': 0, 'EW': 0}
        self.trick_history = []
        self.coach_feedback = "Game started. Choose your bids carefully!"

    def get_hcp(self, seat):
        return sum(HCP.get(c['rank'], 0) for c in self.hands[seat])

    def get_suit_lengths(self, seat):
        lengths = {s: 0 for s in SUITS}
        for c in self.hands[seat]:
            lengths[c['suit']] += 1
        return lengths

    def bid_value(self, level, strain):
        return int(level) * 5 + STRAINS.index(strain)

    # --- Coaching / AI Auction Engine ---
    def evaluate_bid(self, seat):
        points = self.get_hcp(seat)
        lengths = self.get_suit_lengths(seat)
        partner = PARTNERS[seat]
        partner_bids = [b for b in self.bids if b['seat'] == partner and b['bid'] not in ['PASS', 'X', 'XX']]

        # Rule 1: No bids made yet (Opening)
        if not self.highest_bid:
            if points < 12:
                return {'best': 'PASS', 'reason': f"You hold only {points} HCP (less than the standard 12 HCP required to open)."}
            elif 15 <= points <= 17 and all(1 < l < 6 for l in lengths.values()):
                return {'best': '1NT', 'reason': f"Balanced hand distribution with {points} HCP fits standard 1NT opening."}
            elif lengths['♠'] >= 5:
                return {'best': '1♠', 'reason': f"5-card Spade suit with opening strength ({points} HCP)."}
            elif lengths['♥'] >= 5:
                return {'best': '1♥', 'reason': f"5-card Heart suit with opening strength ({points} HCP)."}
            elif lengths['♦'] >= 4:
                return {'best': '1♦', 'reason': f"Better minor opening showing 4+ Diamonds ({points} HCP)."}
            else:
                return {'best': '1♣', 'reason': f"Standard minor opening showing 3+ Clubs and {points} HCP."}

        # Rule 2: Partner opened
        if partner_bids:
            p_bid = partner_bids[-1]['bid']
            p_strain = p_bid[1:] if len(p_bid) > 1 else ''
            
            if points < 6:
                return {'best': 'PASS', 'reason': f"Holding only {points} HCP; pass to keep partner from getting too high."}
            
            # Supporting partner's major
            if p_strain in ['♠', '♥'] and lengths.get(p_strain, 0) >= 3:
                target_level = '2' if points <= 9 else ('3' if points <= 11 else '4')
                bid_str = f"{target_level}{p_strain}"
                if not self.highest_bid or self.bid_value(target_level, p_strain) > self.bid_value(self.highest_bid['level'], self.highest_bid['strain']):
                    return {'best': bid_str, 'reason': f"Fit found in {p_strain} with {points} HCP (supporting partner)."}

            if 6 <= points <= 9 and self.highest_bid['level'] == 1:
                return {'best': '1NT', 'reason': f"6-9 HCP with no primary major fit. 1NT shows minimum responding values."}

        # Default fallback
        return {'best': 'PASS', 'reason': "No clear forcing bid or fit; passing preserves board discipline."}

    # --- Coaching / AI Card Play Engine ---
    def evaluate_card(self, seat):
        hand = self.hands[seat]
        if not hand:
            return None, "No cards remaining."

        trump = self.contract['strain'] if self.contract else None

        # Leading to a trick
        if not self.current_trick:
            # Prefer aces or high honors
            honors = [c for c in hand if c['rank'] in ['A', 'K', 'Q']]
            if honors:
                best = max(honors, key=lambda c: c['val'])
                return best, f"Lead high honor ({best['rank']}{best['suit']}) to establish trick winners."
            best = max(hand, key=lambda c: c['val'])
            return best, f"Lead top of your holding in {best['suit']}."

        # Following suit
        lead_suit = self.current_trick[0]['card']['suit']
        following_cards = [c for c in hand if c['suit'] == lead_suit]

        if following_cards:
            highest_in_trick = max(
                self.current_trick,
                key=lambda p: p['card']['val'] if p['card']['suit'] == lead_suit else -1
            )
            winning = [c for c in following_cards if c['val'] > highest_in_trick['card']['val']]
            if winning:
                best = min(winning, key=lambda c: c['val'])
                return best, f"Cover and win the trick economically with {best['rank']}{best['suit']}."
            else:
                best = min(following_cards, key=lambda c: c['val'])
                return best, f"Cannot beat the current winner; duck low with {best['rank']}{best['suit']}."

        # Void in led suit (Trump or Discard)
        if trump and trump != 'NT':
            trumps = [c for c in hand if c['suit'] == trump]
            if trumps:
                best = min(trumps, key=lambda c: c['val'])
                return best, f"Ruff (trump) the trick with your lowest trump ({best['rank']}{best['suit']})."

        # Discard lowest card
        best = min(hand, key=lambda c: c['val'])
        return best, f"Void in {lead_suit}; discard low ({best['rank']}{best['suit']}) to preserve high winners."

game = BridgeGame()

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def on_join(data):
    role = data.get('role')
    if role in ['South', 'North']:
        game.players[role] = True
        join_room('bridge_room')
        send_game_state()

@socketio.on('make_bid')
def on_bid(data):
    seat = SEATS[game.turn_idx]
    bid = data.get('bid')

    # Evaluate Bid
    eval_res = game.evaluate_bid(seat)
    is_optimal = (bid == eval_res['best'])
    status = 'best' if is_optimal else 'acceptable'
    feedback = f"{'Optimal bid!' if is_optimal else 'Alternative choice.'} {eval_res['reason']}"

    if bid == 'PASS':
        game.consecutive_passes += 1
        game.bids.append({'seat': seat, 'bid': 'PASS', 'feedback': feedback, 'status': status})
    else:
        level = int(bid[0])
        strain = bid[1:]
        game.highest_bid = {'level': level, 'strain': strain, 'seat': seat}
        game.consecutive_passes = 0
        game.bids.append({'seat': seat, 'bid': bid, 'feedback': feedback, 'status': status})

    # Check for Passed Out hand (4 initial passes)
    if len(game.bids) == 4 and all(b['bid'] == 'PASS' for b in game.bids):
        game.phase = 'PASSED_OUT'
        game.coach_feedback = "All four players passed! Hand passed out. Dealing fresh hands..."
        send_game_state()
        socketio.sleep(2)
        game.reset_game(next_dealer=True)
        send_game_state()
        check_bot_turn()
        return

    # Check for Auction Completion (3 consecutive passes after an opening bid)
    if game.consecutive_passes == 3 and game.highest_bid:
        finalize_auction()
    else:
        game.turn_idx = (game.turn_idx + 1) % 4

    send_game_state()
    check_bot_turn()

def finalize_auction():
    winning_bid = game.highest_bid
    strain = winning_bid['strain']
    winning_side = ['North', 'South'] if winning_bid['seat'] in ['North', 'South'] else ['East', 'West']

    # Declarer is the FIRST player from the winning partnership to bid that strain
    declarer = winning_bid['seat']
    for b in game.bids:
        if b['seat'] in winning_side and b['bid'] not in ['PASS', 'X', 'XX'] and b['bid'][1:] == strain:
            declarer = b['seat']
            break

    dummy = PARTNERS[declarer]
    lead_seat = LHO[declarer]

    game.contract = {
        'level': winning_bid['level'],
        'strain': strain,
        'declarer': declarer,
        'dummy': dummy,
        'target': 6 + winning_bid['level']
    }
    game.phase = 'PLAY'
    game.dummy_revealed = False
    game.turn_idx = SEATS.index(lead_seat)
    game.coach_feedback = f"Auction complete! Contract: {game.contract['level']}{game.contract['strain']} by {declarer}. {lead_seat} makes opening lead."

@socketio.on('play_card')
def on_play_card(data):
    current_seat = SEATS[game.turn_idx]
    played_card = data.get('card')
    card_source = data.get('source_seat', current_seat)

    # Lead suit follow rule
    lead_suit = game.current_trick[0]['card']['suit'] if game.current_trick else None
    has_suit = any(c['suit'] == lead_suit for c in game.hands[card_source]) if lead_suit else False

    if lead_suit and has_suit and played_card['suit'] != lead_suit:
        emit('error_message', {'msg': f'Must follow suit ({lead_suit})!'})
        return

    # Card Coach Evaluation
    best_card, reason = game.evaluate_card(card_source)
    is_best = (best_card and played_card['suit'] == best_card['suit'] and played_card['rank'] == best_card['rank'])
    status = 'best' if is_best else 'acceptable'
    feedback = f"{'Optimal play.' if is_best else 'Alternative play.'} {reason}"

    # Remove card from hand
    game.hands[card_source] = [
        c for c in game.hands[card_source]
        if not (c['suit'] == played_card['suit'] and c['rank'] == played_card['rank'])
    ]

    game.current_trick.append({
        'seat': card_source,
        'card': played_card,
        'feedback': feedback,
        'status': status
    })

    # Dummy is revealed right after the opening lead
    if not game.dummy_revealed:
        game.dummy_revealed = True

    # Check if trick is finished (4 cards)
    if len(game.current_trick) == 4:
        send_game_state()
        socketio.sleep(1.2)
        resolve_trick()
    else:
        game.turn_idx = (game.turn_idx + 1) % 4

    send_game_state()
    check_bot_turn()

def resolve_trick():
    lead_suit = game.current_trick[0]['card']['suit']
    trump = game.contract['strain']

    def card_strength(play):
        c = play['card']
        if trump != 'NT' and c['suit'] == trump:
            return 100 + c['val']
        elif c['suit'] == lead_suit:
            return c['val']
        return -1

    winner_play = max(game.current_trick, key=card_strength)
    winner_seat = winner_play['seat']

    if winner_seat in ['North', 'South']:
        game.tricks_won['NS'] += 1
    else:
        game.tricks_won['EW'] += 1

    game.trick_history.append({'winner': winner_seat, 'cards': list(game.current_trick)})
    game.current_trick = []
    game.turn_idx = SEATS.index(winner_seat)

    # Check if all 13 tricks are done
    if sum(game.tricks_won.values()) == 13:
        game.phase = 'HAND_OVER'
        ns_needed = game.contract['target'] if game.contract['declarer'] in ['North', 'South'] else (14 - game.contract['target'])
        won = game.tricks_won['NS'] if game.contract['declarer'] in ['North', 'South'] else game.tricks_won['EW']
        success = won >= game.contract['target']
        game.coach_feedback = f"Hand completed! Contract {game.contract['level']}{game.contract['strain']} {'MADE' if success else 'DEFEATED'} ({won}/{game.contract['target']} tricks)."

@socketio.on('new_deal')
def on_new_deal():
    game.reset_game(next_dealer=True)
    send_game_state()
    check_bot_turn()

def check_bot_turn():
    if game.phase not in ['BIDDING', 'PLAY']:
        return

    current_seat = SEATS[game.turn_idx]
    
    # In play phase: if current seat is dummy, Declarer plays for Dummy
    acting_seat = current_seat
    if game.phase == 'PLAY' and current_seat == game.contract['dummy']:
        acting_seat = game.contract['declarer']

    # If the entity responsible for playing is a Bot (East or West)
    if acting_seat in ['East', 'West']:
        socketio.sleep(0.9)
        if game.phase == 'BIDDING':
            eval_res = game.evaluate_bid(current_seat)
            on_bid({'bid': eval_res['best']})
        elif game.phase == 'PLAY':
            best_card, _ = game.evaluate_card(current_seat)
            if best_card:
                on_play_card({'card': best_card, 'source_seat': current_seat})

def send_game_state():
    current_seat = SEATS[game.turn_idx]
    advice = {'best': 'None', 'reason': ''}
    
    if game.phase == 'BIDDING':
        advice = game.evaluate_bid(current_seat)
    elif game.phase == 'PLAY':
        card, reason = game.evaluate_card(current_seat)
        if card:
            advice = {'best': f"{card['rank']}{card['suit']}", 'reason': reason}

    state = {
        'phase': game.phase,
        'current_seat': current_seat,
        'dealer': SEATS[game.dealer_idx],
        'hands': game.hands,
        'bids': game.bids,
        'highest_bid': game.highest_bid,
        'contract': game.contract,
        'dummy_revealed': game.dummy_revealed,
        'trick': game.current_trick,
        'tricks_won': game.tricks_won,
        'feedback': game.coach_feedback,
        'advice': advice
    }
    socketio.emit('game_update', state)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
