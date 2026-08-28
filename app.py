import random
from flask import Flask, render_template
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sayc-bridge-secret-key-2026'
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
        self.mode = 'single'  # 'single' (1 human + 3 bots) or 'multi' (2 humans + 2 bots)
        self.dealer_idx = 2    # South starts as dealer
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

        self.phase = 'BIDDING'
        self.turn_idx = self.dealer_idx
        self.bids = []
        self.highest_bid = None
        self.consecutive_passes = 0
        
        self.contract = None
        self.dummy_revealed = False
        self.current_trick = []
        self.tricks_won = {'NS': 0, 'EW': 0}
        self.trick_history = []
        self.coach_feedback = "Hand ready. Standard American Yellow Card (SAYC) active."

    def is_bot(self, seat):
        if self.mode == 'single':
            return seat in ['North', 'East', 'West']
        return seat in ['East', 'West']

    def get_controller(self, seat):
        """Returns who has decision authority for `seat`. Declarer controls Dummy."""
        if self.phase == 'PLAY' and self.contract and seat == self.contract['dummy']:
            return self.contract['declarer']
        return seat

    def get_hcp(self, seat):
        return sum(HCP.get(c['rank'], 0) for c in self.hands[seat])

    def get_suit_lengths(self, seat):
        lengths = {s: 0 for s in SUITS}
        for c in self.hands[seat]:
            lengths[c['suit']] += 1
        return lengths

    def bid_value(self, level, strain):
        return int(level) * 5 + STRAINS.index(strain)

    # --- Comprehensive SAYC Bidding Evaluator ---
    def evaluate_bid(self, seat):
        points = self.get_hcp(seat)
        lengths = self.get_suit_lengths(seat)
        partner = PARTNERS[seat]
        partner_bids = [b for b in self.bids if b['seat'] == partner and b['bid'] not in ['PASS', 'X', 'XX']]

        # 1. Opening Bids (No bids made yet)
        if not self.highest_bid:
            if 15 <= points <= 17 and all(1 < l < 6 for l in lengths.values()):
                return {'best': '1NT', 'reason': f"SAYC: 15–17 HCP with a balanced hand opens 1NT."}
            elif 20 <= points <= 21 and all(1 < l < 6 for l in lengths.values()):
                return {'best': '2NT', 'reason': f"SAYC: 20–21 HCP with a balanced hand opens 2NT."}
            elif points >= 12:
                # 5-card majors
                if lengths['♠'] >= 5 and lengths['♠'] >= lengths['♥']:
                    return {'best': '1♠', 'reason': f"SAYC: 12+ HCP with 5+ Spades opens 1♠."}
                elif lengths['♥'] >= 5:
                    return {'best': '1♥', 'reason': f"SAYC: 12+ HCP with 5+ Hearts opens 1♥."}
                # Better Minor
                elif lengths['♦'] >= 4 and lengths['♦'] >= lengths['♣']:
                    return {'best': '1♦', 'reason': f"SAYC: Minor suit opening showing 4+ Diamonds ({points} HCP)."}
                else:
                    return {'best': '1♣', 'reason': f"SAYC: Standard minor suit opening showing 3+ Clubs ({points} HCP)."}
            elif 5 <= points <= 10:
                # Weak Two bids (6-card suit with some honor strength)
                for s in ['♠', '♥', '♦']:
                    if lengths[s] == 6:
                        return {'best': f"2{s}", 'reason': f"SAYC Weak Two: 5–10 HCP with a 6-card {s} suit."}
            return {'best': 'PASS', 'reason': f"SAYC: Pass with fewer than 12 HCP without a weak two suit ({points} HCP)."}

        # 2. Responding to Partner's 1NT Opening
        if partner_bids and partner_bids[-1]['bid'] == '1NT':
            has_4c_major = (lengths['♠'] >= 4 or lengths['♥'] >= 4)
            if points >= 8 and has_4c_major:
                return {'best': '2♣', 'reason': "SAYC Stayman: Asking opener for a 4-card major with 8+ HCP."}
            elif 8 <= points <= 9:
                return {'best': '2NT', 'reason': "SAYC: 8–9 HCP invitational to 3NT."}
            elif 10 <= points <= 15:
                return {'best': '3NT', 'reason': f"SAYC: 10–15 HCP balanced closes game in 3NT."}
            elif points < 8:
                return {'best': 'PASS', 'reason': f"SAYC: Pass with weak hand (<8 HCP) opposite 1NT."}

        # 3. Responding to Partner's 1-Major (1♥ / 1♠)
        if partner_bids and partner_bids[-1]['bid'] in ['1♥', '1♠']:
            p_major = partner_bids[-1]['bid'][1]
            if lengths[p_major] >= 3:
                if 6 <= points <= 9:
                    return {'best': f"2{p_major}", 'reason': f"SAYC: Simple raise to 2{p_major} (6–9 points with 3+ support)."}
                elif 10 <= points <= 11:
                    return {'best': f"3{p_major}", 'reason': f"SAYC: Limit raise to 3{p_major} (10–11 points with 3+ support)."}
                elif points >= 12:
                    return {'best': f"4{p_major}", 'reason': f"SAYC: Game raise to 4{p_major} with 12+ points and fit."}

        # 4. General fallback
        return {'best': 'PASS', 'reason': "SAYC: Pass preserves auction safety with no clear forcing response or fit."}

    # --- Card Play Evaluator ---
    def evaluate_card(self, seat):
        hand = self.hands[seat]
        if not hand:
            return None, "No cards remaining."

        trump = self.contract['strain'] if self.contract else None

        # Trick Leader
        if not self.current_trick:
            honors = [c for c in hand if c['rank'] in ['A', 'K', 'Q']]
            if honors:
                best = max(honors, key=lambda c: c['val'])
                return best, f"Lead high honor ({best['rank']}{best['suit']}) to take immediate control."
            best = max(hand, key=lambda c: c['val'])
            return best, f"Lead top of your holding in {best['suit']}."

        # Following Suit
        lead_suit = self.current_trick[0]['card']['suit']
        following = [c for c in hand if c['suit'] == lead_suit]

        if following:
            highest_in_trick = max(
                self.current_trick,
                key=lambda p: p['card']['val'] if p['card']['suit'] == lead_suit else -1
            )
            winners = [c for c in following if c['val'] > highest_in_trick['card']['val']]
            if winners:
                best = min(winners, key=lambda c: c['val'])
                return best, f"Follow suit and win the trick cheaply with {best['rank']}{best['suit']}."
            else:
                best = min(following, key=lambda c: c['val'])
                return best, f"Cannot beat the current winner; duck low with {best['rank']}{best['suit']}."

        # Trumping or Discarding
        if trump and trump != 'NT':
            trumps = [c for c in hand if c['suit'] == trump]
            if trumps:
                best = min(trumps, key=lambda c: c['val'])
                return best, f"Ruff (trump) the trick with your lowest trump ({best['rank']}{best['suit']})."

        best = min(hand, key=lambda c: c['val'])
        return best, f"Void in {lead_suit}; sluff your lowest card ({best['rank']}{best['suit']})."

game = BridgeGame()

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('set_mode')
def on_set_mode(data):
    game.mode = data.get('mode', 'single')
    game.reset_game(next_dealer=False)
    send_game_state()
    check_bot_turn()

@socketio.on('join_game')
def on_join(data):
    join_room('bridge_room')
    send_game_state()

@socketio.on('make_bid')
def on_bid(data):
    seat = SEATS[game.turn_idx]
    bid = data.get('bid')

    eval_res = game.evaluate_bid(seat)
    is_optimal = (bid == eval_res['best'])
    feedback = f"{'Optimal Bid!' if is_optimal else 'Acceptable / Alternative.'} {eval_res['reason']}"

    if bid == 'PASS':
        game.consecutive_passes += 1
        game.bids.append({'seat': seat, 'bid': 'PASS', 'feedback': feedback, 'status': 'best' if is_optimal else 'acceptable'})
    else:
        level = int(bid[0])
        strain = bid[1:]
        game.highest_bid = {'level': level, 'strain': strain, 'seat': seat}
        game.consecutive_passes = 0
        game.bids.append({'seat': seat, 'bid': bid, 'feedback': feedback, 'status': 'best' if is_optimal else 'acceptable'})

    # 4 consecutive passes to open = Passed Out (Auto Redeal)
    if len(game.bids) == 4 and all(b['bid'] == 'PASS' for b in game.bids):
        game.phase = 'PASSED_OUT'
        game.coach_feedback = "All four players passed out! Dealing a fresh hand..."
        send_game_state()
        socketio.sleep(2)
        game.reset_game(next_dealer=True)
        send_game_state()
        check_bot_turn()
        return

    # 3 consecutive passes after a bid = Contract Finalized
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
    game.coach_feedback = f"Contract: {game.contract['level']}{game.contract['strain']} by {declarer}. Opening lead by {lead_seat}."

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

    best_card, reason = game.evaluate_card(card_source)
    is_best = (best_card and played_card['suit'] == best_card['suit'] and played_card['rank'] == best_card['rank'])
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
        'status': 'best' if is_best else 'acceptable'
    })

    # Reveal Dummy immediately after opening lead
    if not game.dummy_revealed:
        game.dummy_revealed = True

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

    if sum(game.tricks_won.values()) == 13:
        game.phase = 'HAND_OVER'
        won = game.tricks_won['NS'] if game.contract['declarer'] in ['North', 'South'] else game.tricks_won['EW']
        success = won >= game.contract['target']
        game.coach_feedback = f"Hand Complete! Contract {game.contract['level']}{game.contract['strain']} {'MADE' if success else 'DEFEATED'} ({won}/{game.contract['target']} tricks)."

@socketio.on('new_deal')
def on_new_deal():
    game.reset_game(next_dealer=True)
    send_game_state()
    check_bot_turn()

def check_bot_turn():
    if game.phase not in ['BIDDING', 'PLAY']:
        return

    current_seat = SEATS[game.turn_idx]
    controller = game.get_controller(current_seat)

    # Only act automatically if the controlling entity is a bot
    if game.is_bot(controller):
        socketio.sleep(0.8)
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
        'mode': game.mode,
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
