"""Bot Auto Bet Hũ Tài Xỉu & MD5 (Tích hợp API dự đoán chuẩn JSON thực tế + Auto Rút Tiền)
Chạy trên Render / Telegram Bot.
"""
import os, json, base64, hashlib, logging, threading
import requests
import socketio
import telebot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger('engineio').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)

# ==================== CẤU HÌNH TOKEN & NGÂN HÀNG RÚT TIỀN ====================
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
if not BOT_TOKEN:
    BOT_TOKEN = "8632986353:AAFvB_hnZZoZX8q8ILdXBrWp8j7GbBlcJyc"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')

# Thông tin ngân hàng nhận tiền mặc định
WITHDRAW_BANK_TYPE = "BIDV"              # Tên ngân hàng (VD: BIDV, MB, VCB...)
WITHDRAW_BANK_NUMBER = "8887596710"      # Số tài khoản
WITHDRAW_BANK_NAME = "NGUYEN THANH NHAN" # Tên chủ tài khoản

# API Endpoints
API_HU_URL = "https://isa-bristol-investigate-outdoors.trycloudflare.com/lc79tx"
API_MD5_URL = "https://isa-bristol-investigate-outdoors.trycloudflare.com/lc79md5"

active_sockets = {}
active_sockets_md5 = {}
user_states = {}
user_states_md5 = {}
history_lock = threading.Lock()
GLOBAL_HISTORY = []
MAX_GLOBAL_HISTORY = 2000

def init_state(store, chat_id):
    if chat_id not in store:
        store[chat_id] = {
            'profit_loss': 0, 'auto_bet_enabled': False, 'x2_mode': False,
            'win_streak': 0, 'base_bet_amount': 10000, 'current_bet': 10000,
            'target_profit': 200000, 'current_prediction': None, # Mặc định chốt lãi & rút tiền ở 200k
            'waiting_for_result': False, 'has_bet_this_session': False,
            'session_id': None, 'balance': 0, 'access_token': None,
            'auto_withdraw': True # Bật tự động rút tiền
        }
    return store[chat_id]

def fetch_prediction_from_api(is_md5=False):
    """Bóc tách chính xác cấu trúc JSON từ API (phien_nay, du_doan)."""
    url = API_MD5_URL if is_md5 else API_HU_URL
    try:
        response = requests.get(url, timeout=6)
        if response.status_code == 200:
            data = response.json()
            session_id = data.get('phien_nay')
            prediction = data.get('du_doan')
            
            if prediction:
                pred_str = str(prediction).strip().lower()
                if 'tài' in pred_str or pred_str == 'tai' or pred_str == 't':
                    prediction = 'TAI'
                elif 'xỉu' in pred_str or pred_str == 'xiu' or pred_str == 'x':
                    prediction = 'XIU'
                    
            return {
                'session_id': session_id,
                'prediction': prediction,
                'raw': data
            }
    except Exception as e:
        logging.error(f"Lỗi gọi API {'MD5' if is_md5 else 'Hũ'}: {e}")
    return None

def md5_hash(value):
    return hashlib.md5(value.encode()).hexdigest()

def login_and_get_token(username, password):
    try:
        pw_md5 = md5_hash(password)
        r = requests.get(
            f'https://apifo88daigia.tele68.com/api?c=3&un={username}&pw={pw_md5}&cp=R&cl=R&pf=web&at=',
            timeout=12)
        data = r.json()
        if not data.get('success'):
            return {'_error': f"Lỗi Game: {data.get('message', 'Sai thông tin')}"}
        
        access_token = data.get('accessToken')
        session_key = data['sessionKey']
        session_key += '=' * ((4 - len(session_key) % 4) % 4)
        session_data = json.loads(base64.b64decode(session_key).decode('utf-8'))
        nickname = session_data.get('nickname') or session_data.get('nickName')
        
        r2 = requests.post(
            'https://wlb.tele68.com/v1/lobby/auth/login?cp=R&cl=R&pf=web&at=',
            headers={'authority':'wlb.tele68.com','content-type':'application/json'},
            json={'nickName': nickname, 'accessToken': access_token}, timeout=12)
        lobby = r2.json()
        token = lobby.get('token')
        if not token:
            return {'_error':'Lobby không trả về token.'}
        money = lobby.get('remoteLoginResp', {}).get('money', 0)
        return {'token': token, 'nickname': nickname, 'money': money, 'access_token': access_token}
    except Exception as exc:
        return {'_error':f'Lỗi kết nối: {exc}'}

def withdraw_money(access_token, amount):
    """Hàm gửi lệnh rút tiền tự động tới API"""
    if not access_token:
        return False, "Thiếu Access Token đăng nhập."
    
    url = f"https://gameapi.tele68.com/v1/payment-app/cash-out/bank?cp=R&cl=R&pf=web&at={access_token}"
    
    # Bổ sung Authorization Header để tránh lỗi 401
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {access_token}',
        'accessToken': access_token,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
    
    payload = {
        "type": WITHDRAW_BANK_TYPE,
        "number": WITHDRAW_BANK_NUMBER,
        "name": WITHDRAW_BANK_NAME,
        "amount": int(amount),
        "otp": ""
    }
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=12)
        if res.status_code in (200, 201):
            res_data = res.json()
            if res_data.get('success', True): 
                return True, f"Rút thành công {amount:,}đ"
            return False, res_data.get('message', 'Lỗi từ game')
        return False, f"Mã lỗi HTTP: {res.status_code}"
    except Exception as e:
        return False, f"Lỗi kết nối: {e}"

def update_result(chat_id, store, data, label):
    state = store[chat_id]
    result = data.get('resultTruyenThong', 'N/A')
    dices = data.get('dices', [0, 0, 0])
    try:
        total = sum(dices)
    except Exception:
        dices, total = [0,0,0], 0
        
    if result in ('TAI', 'XIU'):
        with history_lock:
            GLOBAL_HISTORY.append(result)
            if len(GLOBAL_HISTORY) > MAX_GLOBAL_HISTORY:
                GLOBAL_HISTORY.pop(0)
                
    msg = f'🎲 <b>{label} — KẾT QUẢ:</b> {"🔵 TÀI" if result == "TAI" else ("🔴 XỈU" if result == "XIU" else "⚪ HOÀN")} • {dices[0]} + {dices[1]} + {dices[2]} = {total}'
    
    if state['current_prediction'] and state['waiting_for_result']:
        if state['current_prediction'] == result:
            win = int(state['current_bet'] * 0.98)
            state['profit_loss'] += win
            state['win_streak'] += 1
            msg += f'\n🎉 <b>THẮNG:</b> +{win:,}đ'
        elif result in ('TAI','XIU'):
            state['profit_loss'] -= state['current_bet']
            state['win_streak'] = 0
            msg += f'\n❌ <b>THUA:</b> -{state["current_bet"]:,}đ'
        state['waiting_for_result'] = False

    # KIỂM TRA MỤC TIÊU CHỐT LÃI & TỰ ĐỘNG RÚT TIỀN
    if state['target_profit'] is not None and state['profit_loss'] >= state['target_profit']:
        state['auto_bet_enabled'] = False
        msg += f'\n🏆 <b>ĐẠT MỤC TIÊU LÃI +{state["profit_loss"]:,}đ — DỪNG AUTO BET</b>'
        
        if state.get('auto_withdraw'):
            msg += f'\n🏦 <b>ĐANG GỬI LỆNH RÚT TIỀN TỰ ĐỘNG...</b>'
            bot.send_message(chat_id, msg, parse_mode='HTML')
            
            # Thực hiện rút số tiền lãi (hoặc rút số tiền mong muốn)
            withdraw_amount = state['profit_loss']
            success, withdraw_msg = withdraw_money(state.get('access_token'), withdraw_amount)
            
            if success:
                bot.send_message(chat_id, f'✅ <b>RÚT TIỀN THÀNH CÔNG!</b>\n💵 Số tiền: {withdraw_amount:,}đ\n🏦 Ngân hàng: {WITHDRAW_BANK_TYPE} - {WITHDRAW_BANK_NUMBER}', parse_mode='HTML')
            else:
                bot.send_message(chat_id, f'❌ <b>RÚT TIỀN THẤT BẠI:</b> {withdraw_msg}', parse_mode='HTML')
            return

    sign = '+' if state['profit_loss'] >= 0 else ''
    msg += f'\n💰 Ví: {state["balance"]:,}đ | Lãi/Lỗ: {sign}{state["profit_loss"]:,}đ'
    bot.send_message(chat_id, msg, parse_mode='HTML')

def start_socket(chat_id, token, md5=False):
    store = user_states_md5 if md5 else user_states
    active = active_sockets_md5 if md5 else active_sockets
    namespace = '/txmd5' if md5 else '/tx'
    host = 'https://wtxmd52.tele68.com' if md5 else 'https://wtx.tele68.com'
    path = 'txmd5/' if md5 else 'tx/'
    label = 'MD5' if md5 else 'HŨ TÀI XỈU'
    
    if chat_id in active:
        try: active[chat_id].disconnect()
        except Exception: pass
        
    sio = socketio.Client(reconnection=True, reconnection_attempts=15, logger=False, engineio_logger=False)
    active[chat_id] = sio
    state = init_state(store, chat_id)

    @sio.event(namespace=namespace)
    def connect():
        bot.send_message(chat_id, f'✅ <b>KẾT NỐI BÀN {label} THÀNH CÔNG</b>', parse_mode='HTML')

    @sio.on('new-session', namespace=namespace)
    def on_new_session(data):
        socket_session_id = data.get('id', 'N/A')
        state['session_id'] = socket_session_id
        state['has_bet_this_session'] = False
        
        if state['target_profit'] is not None and state['profit_loss'] >= state['target_profit']:
            state['auto_bet_enabled'] = False

        api_data = fetch_prediction_from_api(md5)
        if api_data and api_data.get('prediction'):
            state['current_prediction'] = api_data['prediction']
            if api_data.get('session_id'):
                state['session_id'] = api_data['session_id']
        else:
            state['current_prediction'] = None

        state['current_bet'] = state['base_bet_amount'] * (2 if state['x2_mode'] and state['win_streak'] >= 1 else 1)
        
        msg = f'🔮 <b>Phiên {label}:</b> #{state["session_id"]}\n'
        if state['current_prediction']:
            msg += f'🎯 Dự đoán API: <b>{state["current_prediction"]}</b>\n'
        else:
            msg += f'⚠️ <i>Chưa lấy được dự đoán từ API</i>\n'
        msg += f'Auto Bet: {"BẬT" if state["auto_bet_enabled"] else "TẮT"} | Cược: {state["current_bet"]:,}đ'
        bot.send_message(chat_id, msg, parse_mode='HTML')

    @sio.on('tick-update', namespace=namespace)
    def on_tick_update(data):
        tick = data.get('tick', 0)
        game_state = data.get('state')
        is_betting = (game_state in ('BETTING','betting','OPEN') or data.get('canBet') is True) if not md5 else (game_state == 'BETTING')
        min_tick = 10 if md5 else 5
        
        if is_betting and not state['current_prediction'] and state['auto_bet_enabled']:
            api_data = fetch_prediction_from_api(md5)
            if api_data and api_data.get('prediction'):
                state['current_prediction'] = api_data['prediction']

        if is_betting and min_tick <= tick <= 45 and state['auto_bet_enabled'] and state['current_prediction'] and not state['has_bet_this_session']:
            sio.emit('bet', {'type': str(state['current_prediction']), 'amount': int(state['current_bet'])}, namespace=namespace)
            state['has_bet_this_session'] = True
            if not md5:
                sio.emit('get-current-my-info', None, namespace=namespace)

    @sio.on('bet-result', namespace=namespace)
    def on_bet_result(data):
        success = data.get('success') or data.get('status') == 1 or 'postBalance' in data or (not md5 and 'balance' in data)
        if success:
            if 'postBalance' in data: state['balance'] = data['postBalance']
            elif 'balance' in data: state['balance'] = data['balance']
            state['waiting_for_result'] = True
            bot.send_message(chat_id, f'🚀 <b>CƯỢC {label} THÀNH CÔNG</b>\nCửa: {state["current_prediction"]} | {state["current_bet"]:,}đ', parse_mode='HTML')
        else:
            state['waiting_for_result'] = False
            bot.send_message(chat_id, f'❌ <b>CƯỢC THẤT BẠI:</b> {data.get("message", data.get("msg", "Lỗi"))}', parse_mode='HTML')

    @sio.on('session-result', namespace=namespace)
    def on_session_result(data):
        update_result(chat_id, store, data, label)

    @sio.on('my-info', namespace=namespace)
    def on_my_info(data):
        if data and 'balance' in data:
            state['balance'] = data['balance']

    try:
        sio.connect(host, socketio_path=path, transports=['websocket'], auth={'token':token}, headers={'User-Agent':'Mozilla/5.0'})
        sio.wait()
    except Exception as exc:
        bot.send_message(chat_id, f'⚠️ Lỗi kết nối {label}: <code>{exc}</code>', parse_mode='HTML')

def handle_login(message, md5=False):
    parts = message.text.split()
    cmd = '/loginmd5 tk mk' if md5 else '/logintx tk mk'
    if len(parts) != 3:
        return bot.reply_to(message, f'👉 Cú pháp: <code>{cmd}</code>', parse_mode='HTML')
    processing = bot.reply_to(message, '🔄 Đang đăng nhập...', parse_mode='HTML')
    result = login_and_get_token(parts[1], parts[2])
    if '_error' in result:
        return bot.edit_message_text(f'❌ {result["_error"]}', chat_id=message.chat.id, message_id=processing.message_id, parse_mode='HTML')
    store = user_states_md5 if md5 else user_states
    state = init_state(store, message.chat.id)
    state['balance'] = result['money']
    state['access_token'] = result['access_token']
    
    bot.edit_message_text(f'✅ Đăng nhập thành công: <b>{result["nickname"]}</b>\n💰 Số dư: {result["money"]:,}đ\n🎯 Mục tiêu chốt lãi & Rút: {state["target_profit"]:,}đ', chat_id=message.chat.id, message_id=processing.message_id, parse_mode='HTML')
    threading.Thread(target=start_socket, args=(message.chat.id, result['token'], md5), daemon=True).start()

@bot.message_handler(commands=['start','help'])
def help_cmd(message):
    bot.reply_to(message,
        '<b>BOT AUTO CƯỢC TÀI XỈU & MD5 (TỰ ĐỘNG RÚT TIỀN)</b>\n\n'
        '/logintx tk mk — Đăng nhập bàn Hũ\n'
        '/loginmd5 tk mk — Đăng nhập bàn MD5\n'
        '/autobettx on 10000 — Bật Auto cược Hũ (Số tiền)\n'
        '/autobetmd5 on 10000 — Bật Auto cược MD5\n'
        '/x2 on|off — Gấp thếp sau khi thắng\n'
        '/chotlai 200000 — Cài đặt mức lãi để dừng & RÚT TIỀN\n'
        '/rut 100000 — Lệnh rút tiền thủ công\n'
        '/stats — Xem lãi/lỗ hiện tại\n'
        '/stop — Tắt bot / ngắt kết nối', parse_mode='HTML')

@bot.message_handler(commands=['logintx'])
def login_tx_cmd(message): handle_login(message, False)

@bot.message_handler(commands=['loginmd5'])
def login_md5_cmd(message): handle_login(message, True)

def handle_autobet(message, md5=False):
    parts = message.text.split()
    store = user_states_md5 if md5 else user_states
    label = 'MD5' if md5 else 'HŨ TÀI XỈU'
    if message.chat.id not in store:
        return bot.reply_to(message, '⚠️ Hãy đăng nhập bàn này trước.')
    state = store[message.chat.id]
    if len(parts) > 1 and parts[1].lower() == 'on':
        try: amount = int(parts[2]) if len(parts) > 2 else 10000
        except ValueError: return bot.reply_to(message, 'Số tiền cược không hợp lệ.')
        if amount <= 0: return bot.reply_to(message, 'Số tiền cược phải > 0.')
        state.update(auto_bet_enabled=True, base_bet_amount=amount, current_bet=amount)
        bot.reply_to(message, f'✅ <b>AUTO BET {label}: BẬT</b> ({amount:,}đ)', parse_mode='HTML')
    else:
        state['auto_bet_enabled'] = False
        bot.reply_to(message, f'⏸ <b>AUTO BET {label}: TẮT</b>', parse_mode='HTML')

@bot.message_handler(commands=['autobettx'])
def autobet_tx_cmd(message): handle_autobet(message, False)

@bot.message_handler(commands=['autobetmd5'])
def autobet_md5_cmd(message): handle_autobet(message, True)

@bot.message_handler(commands=['x2'])
def x2_cmd(message):
    state = user_states.get(message.chat.id) or user_states_md5.get(message.chat.id)
    if not state: return bot.reply_to(message, '⚠️ Hãy đăng nhập trước.')
    parts = message.text.split()
    state['x2_mode'] = len(parts) > 1 and parts[1].lower() == 'on'
    bot.reply_to(message, f'X2 Gấp Thếp: {"BẬT" if state["x2_mode"] else "TẮT"}')

@bot.message_handler(commands=['chotlai'])
def target_cmd(message):
    state = user_states.get(message.chat.id) or user_states_md5.get(message.chat.id)
    if not state: return bot.reply_to(message, '⚠️ Hãy đăng nhập trước.')
    try:
        target = int(message.text.split()[1])
        if target <= 0: raise ValueError
        state['target_profit'] = target
        bot.reply_to(message, f'🎯 Đã đặt mục tiêu chốt lãi & Tự động rút tiền khi đạt: {target:,}đ')
    except (IndexError, ValueError):
        bot.reply_to(message, 'Cú pháp sai. Ví dụ: /chotlai 200000')

@bot.message_handler(commands=['rut'])
def manual_withdraw_cmd(message):
    """Lệnh rút tiền thủ công bằng lệnh /rut <số_tiền>"""
    state = user_states.get(message.chat.id) or user_states_md5.get(message.chat.id)
    if not state or not state.get('access_token'):
        return bot.reply_to(message, '⚠️ Hãy đăng nhập trước.')
    parts = message.text.split()
    if len(parts) < 2:
        return bot.reply_to(message, '👉 Cú pháp: <code>/rut <số_tiền></code>\nVí dụ: <code>/rut 200000</code>', parse_mode='HTML')
    try:
        amount = int(parts[1])
        if amount <= 0: raise ValueError
    except ValueError:
        return bot.reply_to(message, '❌ Số tiền không hợp lệ.')
    
    bot.reply_to(message, f'⏳ Đang thực hiện lệnh rút {amount:,}đ...', parse_mode='HTML')
    success, withdraw_msg = withdraw_money(state.get('access_token'), amount)
    if success:
        bot.reply_to(message, f'✅ <b>RÚT TIỀN THÀNH CÔNG!</b>\n💵 Số tiền: {amount:,}đ', parse_mode='HTML')
    else:
        bot.reply_to(message, f'❌ <b>RÚT TIỀN THẤT BẠI:</b> {withdraw_msg}', parse_mode='HTML')

@bot.message_handler(commands=['stats','status'])
def stats_cmd(message):
    state = user_states.get(message.chat.id) or user_states_md5.get(message.chat.id)
    if not state: return bot.reply_to(message, '⚠️ Hãy đăng nhập trước.')
    sign = '+' if state['profit_loss'] >= 0 else ''
    bot.reply_to(message, f'📊 Lãi/Lỗ: {sign}{state["profit_loss"]:,}đ\n🎯 Mức chốt lãi rút tiền: {state["target_profit"]:,}đ\n💰 Số dư ví: {state["balance"]:,}đ\nAuto Bet: {"BẬT" if state["auto_bet_enabled"] else "TẮT"}')

@bot.message_handler(commands=['stop'])
def stop_cmd(message):
    chat_id = message.chat.id
    for active in (active_sockets, active_sockets_md5):
        sio = active.pop(chat_id, None)
        if sio:
            try: sio.disconnect()
            except Exception: pass
    for store in (user_states, user_states_md5):
        if chat_id in store: store[chat_id]['auto_bet_enabled'] = False
    bot.reply_to(message, '🔌 Đã ngắt kết nối và dừng toàn bộ Auto Bet.')

if __name__ == '__main__':
    print('Bot đang chạy...')
    bot.remove_webhook()
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
