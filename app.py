import random
from flask import Flask, render_template
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'bridge-secret-key-123'
socketio = SocketIO(app, cors_allowed_origins="*")

# Card rankings & suit order
SUITS = ['♣', '♦', '♥', '♠']
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
HCP = {'A': 4, 'K': 3, 'Q': 2, 'J': 1}

class BridgeGame:
    def __init__(self):
        self.players = {'South': None, 'North': None}  # Human player slots
        self.seats = ['South', 'West', 'North', 'East']  # West & East are AI bots
        self.reset_game()

    def reset_game(self):
        deck = [{'suit': s, 'rank': r, 'val': RANKS.index(r)} for s in SUITS for r in RANKS]
        random.shuffle(deck)
        
        self.hands = {
            'South': sorted(deck[0:13], key=lambda c: (SUITS.index(c['suit']), c['val'])),
            'West': sorted(deck[13:26], key=lambda c: (SUITS.index(c['suit']), c['val'])),
            'North': sorted(deck[26:39], key=lambda c: (SUITS.index(c['suit']), c['val'])),
            'East': sorted(deck[39:52], key=lambda c: (SUITS.index(c['suit']), c['val']))
        }
        self.phase = 'BIDDING'  # 'BIDDING' or 'PLAY'
        self.turn_idx = 0       # South starts bidding
        self.bids = []
        self.current_trick = []
        self.tricks_won = {'NS': 0, 'EW': 0}
        self.contract = None

    def get_hcp(self, hand):
        return sum(HCP.get(card['rank'], 0) for card in hand)

    def get_suit_lengths(self, hand):
        lengths = {s: 0 for s in SUITS}
        for card in hand:
            lengths[card['suit']] += 1
        return lengths

    # Coach & AI Evaluation for Bids (Standard American 5-Card Majors)
    def evaluate_bid(self, seat):
        hand = self.hands[seat]
        points = self.get_hcp(hand)
        lengths = self.get_suit_lengths(hand)

        if not self.bids or all(b['bid'] == 'PASS' for b in self.bids):
            # Opening Bid logic
            if points < 12:
                return {'best': 'PASS', 'reason': f"You hold only {points} HCP (less than the standard 12 HCP required to open)."}
            elif 15 <= points <= 17 and all(1 < l < 6 for l in lengths.values()):
                return {'best': '1NT', 'reason': f"Balanced hand distribution with {points} HCP matches standard 1NT opening."}
            elif lengths['♠'] >= 5:
                return {'best': '1♠', 'reason': f"5-card Spade suit with opening strength ({points} HCP)."}
            elif lengths['♥'] >= 5:
                return {'best': '1♥', 'reason': f"5-card Heart suit with opening strength ({points} HCP)."}
            elif lengths['♦'] >= 4:
                return {'best': '1♦', 'reason': f"Better minor opening with 4+ Diamonds ({points} HCP)."}
            else:
                return {'best': '1♣', 'reason': f"Standard minor opening showing 3+ Clubs and {points} HCP."}
        else:
            return {'best': 'PASS', 'reason': "Passing here maintains basic auction discipline for this practice hand."}

    # Coach & AI Evaluation for Card Play
    def evaluate_card(self, seat):
        hand = self.hands[seat]
        if not hand:
            return None, "No cards remaining."

        # If leading a new trick
        if not self.current_trick:
            best_card = max(hand, key=lambda c: c['val'])
            return best_card, f"Leading your highest card ({best_card['rank']}{best_card['suit']}) establishes trick pressure."

        # If following to an existing trick
        lead_suit = self.current_trick[0]['card']['suit']
        following_cards = [c for c in hand if c['suit'] == lead_suit]

        if following_cards:
            highest_in_trick = max(
                self.current_trick, 
                key=lambda p: p['card']['val'] if p['card']['suit'] == lead_suit else -1
            )
            winning_moves = [c for c in following_cards if c['val'] > highest_in_trick['card']['val']]

            if winning_moves:
                best_card = min(winning_moves, key=lambda c: c['val'])
                return best_card, f"Follow suit and win the trick economically with {best_card['rank']}{best_card['suit']}."
            else:
                best_card = min(following_cards, key=lambda c: c['val'])
                return best_card, f"Cannot beat the active high card; duck low with {best_card['rank']}{best_card['suit']}."
        else:
            # Void in the led suit — discard lowest card overall
            best_card = min(hand, key=lambda c: c['val'])
            return best_card, f"Void in {lead_suit}; discard your lowest card ({best_card['rank']}{best_card['suit']}) to preserve winners."

game = BridgeGame()

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def on_join(data):
    role = data.get('role')
    if role in ['South', 'North']:
        game.players[role] = True
        join_room('game_room')
        send_game_state()

@socketio.on('make_bid')
def on_bid(data):
    seat = game.seats[game.turn_idx]
    bid = data.get('bid')

    evaluation = game.evaluate_bid(seat)
    is_best = (bid == evaluation['best'])
    
    feedback = "Optimal Bid! " if is_best else "Suboptimal Bid. "
    feedback += evaluation['reason']

    game.bids.append({
        'seat': seat, 
        'bid': bid, 
        'feedback': feedback, 
        'status': 'best' if is_best else 'okay'
    })

    # Close bidding after 4 bids
    if len(game.bids) >= 4:
        game.phase = 'PLAY'
        game.contract = next((b['bid'] for b in reversed(game.bids) if b['bid'] != 'PASS'), '1NT')
        game.turn_idx = 0
    else:
        game.turn_idx = (game.turn_idx + 1) % 4

    send_game_state()
    check_bot_turn()

@socketio.on('play_card')
def on_play_card(data):
    seat = game.seats[game.turn_idx]
    played_card = data.get('card')

    # Enforce follow-suit rule
    lead_suit = game.current_trick[0]['card']['suit'] if game.current_trick else None
    has_suit = any(c['suit'] == lead_suit for c in game.hands[seat]) if lead_suit else False

    if lead_suit and has_suit and played_card['suit'] != lead_suit:
        emit('error_message', {'msg': f'Must follow suit ({lead_suit})!'})
        return

    best_card, reason = game.evaluate_card(seat)
    is_best = (played_card['suit'] == best_card['suit'] and played_card['rank'] == best_card['rank'])
    
    feedback = f"{'Optimal play.' if is_best else 'Acceptable alternative.'} {reason}"

    # Remove the played card from the player's hand
    game.hands[seat] = [
        c for c in game.hands[seat] 
        if not (c['suit'] == played_card['suit'] and c['rank'] == played_card['rank'])
    ]
    
    game.current_trick.append({
        'seat': seat, 
        'card': played_card, 
        'feedback': feedback, 
        'status': 'best' if is_best else 'okay'
    })

    # Evaluate complete trick (4 cards played)
    if len(game.current_trick) == 4:
        socketio.sleep(1)
        lead_suit = game.current_trick[0]['card']['suit']
        valid_plays = [p for p in game.current_trick if p['card']['suit'] == lead_suit]
        winner_play = max(valid_plays, key=lambda p: p['card']['val'])
        winner_seat = winner_play['seat']

        if winner_seat in ['South', 'North']:
            game.tricks_won['NS'] += 1
        else:
            game.tricks_won['EW'] += 1

        game.current_trick = []
        game.turn_idx = game.seats.index(winner_seat)
    else:
        game.turn_idx = (game.turn_idx + 1) % 4

    send_game_state()
    check_bot_turn()

def check_bot_turn():
    current_seat = game.seats[game.turn_idx]
    if current_seat in ['West', 'East']:
        socketio.sleep(0.8)
        if game.phase == 'BIDDING':
            eval_data = game.evaluate_bid(current_seat)
            on_bid({'bid': eval_data['best']})
        elif game.phase == 'PLAY':
            best_card, _ = game.evaluate_card(current_seat)
            if best_card:
                on_play_card({'card': best_card})

def send_game_state():
    current_seat = game.seats[game.turn_idx]
    advice = {}
    if game.phase == 'BIDDING':
        advice = game.evaluate_bid(current_seat)
    else:
        best_card, reason = game.evaluate_card(current_seat)
        advice = {
            'best': f"{best_card['rank']}{best_card['suit']}" if best_card else "None", 
            'reason': reason
        }

    state = {
        'phase': game.phase,
        'current_seat': current_seat,
        'hands': game.hands,
        'bids': game.bids,
        'trick': game.current_trick,
        'tricks_won': game.tricks_won,
        'contract': game.contract,
        'advice': advice
    }
    socketio.emit('game_update', state)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
