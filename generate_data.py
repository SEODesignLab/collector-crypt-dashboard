#!/usr/bin/env python3
"""
Generate dashboard-data.json for the Collector Crypt dashboard.
Called by the monitoring cron (every 2h) before git push.
Pulls fresh wallet stats from Solana RPC and merges with accumulated history.
"""
import requests, json, os, subprocess
from datetime import datetime, timezone

RPC = 'https://solana-mainnet.g.alchemy.com/v2/WsIAMnMfQS4V1SdWpHS7o'
USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
BOT = 'FrY8u2MhPoV3xjSxeZf74ftPMdvthAo9or6fLGwLAXr8'
SELLER = 'Dpua5doi7EKeh9oSEpCLe99o76eFFC5FrrartM95wQBQ'
DASH_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(DASH_DIR, 'data')
DATA_FILE = os.path.join(DATA_DIR, 'dashboard-data.json')
HISTORY_FILE = os.path.join(DATA_DIR, 'history.json')

def rpc_call(method, params, id=1):
    try:
        r = requests.post(RPC, json={'jsonrpc':'2.0','id':id,'method':method,'params':params}, timeout=15)
        return r.json().get('result')
    except Exception:
        return None

def get_usdc_balance(wallet):
    res = rpc_call('getTokenAccountsByOwner', [wallet, {'mint': USDC_MINT}, {'encoding':'jsonParsed'}])
    if not res: return 0.0
    total = 0.0
    for a in res.get('value', []):
        info = a.get('account',{}).get('data',{}).get('parsed',{}).get('info',{})
        total += info.get('tokenAmount',{}).get('uiAmount',0) or 0
    return total

def get_sol_balance(wallet):
    res = rpc_call('getBalance', [wallet])
    if res is None: return 0.0
    if isinstance(res, dict):
        res = res.get('value', 0)
    return (res or 0) / 1e9

def get_token_account_count(wallet):
    res = rpc_call('getTokenAccountsByOwner', [wallet, {'programId':'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'}, {'encoding':'jsonParsed'}])
    if not res: return 0
    accounts = res.get('value', [])
    nonzero = 0
    for a in accounts:
        info = a.get('account',{}).get('data',{}).get('parsed',{}).get('info',{})
        if (info.get('tokenAmount',{}).get('uiAmount',0) or 0) > 0:
            nonzero += 1
    return len(accounts), nonzero

def get_recent_txns(wallet, limit=100):
    res = rpc_call('getSignaturesForAddress', [wallet, {'limit': limit}])
    return [s for s in (res or []) if not s.get('err')]

def analyze_txns(wallet, sigs, max_decode=15):
    """Decode a sample of txns, return instruction counts + events."""
    counts = {}
    events = []  # (type, usdc, asset_mint, ts, buyer/seller, tx_sig)
    decoded = 0
    for s in sigs[:max_decode]:
        tx = rpc_call('getTransaction', [s['signature'], {'maxSupportedTransactionVersion':0,'encoding':'json'}])
        if not tx: continue
        decoded += 1
        logs = tx.get('meta',{}).get('logMessages',[]) or []
        ixs = [l.replace('Program log: Instruction: ','') for l in logs if 'Instruction:' in l]
        for ix in ixs:
            counts[ix] = counts.get(ix,0) + 1
        
        # USDC token flows
        pre_t = tx.get('meta',{}).get('preTokenBalances',[]) or []
        post_t = tx.get('meta',{}).get('postTokenBalances',[]) or []
        usdc_deltas = {}
        for pt in post_t:
            if pt.get('mint') != USDC_MINT: continue
            owner = pt.get('owner','')
            amt = pt.get('uiTokenAmount',{}).get('uiAmount',0) or 0
            pre_amt = 0
            for prt in pre_t:
                if prt.get('mint')==USDC_MINT and prt.get('owner')==owner:
                    pre_amt = prt.get('uiTokenAmount',{}).get('uiAmount',0) or 0
            d = amt - pre_amt
            if abs(d) > 0.001:
                usdc_deltas[owner] = d
        
        ts = datetime.fromtimestamp(tx['blockTime'], tz=timezone.utc).strftime('%m-%d %H:%M') if tx.get('blockTime') else ''
        
        if 'AcceptOfferForCore' in ixs:
            # bot's offer accepted — bot pays USDC (or escrow releases)
            bot_delta = usdc_deltas.get(BOT, 0)
            events.append({'type':'acquisition','time':ts,'usdc':abs(bot_delta),'tx':s['signature'][:44],'wallet':BOT})
        if 'BuyCore' in ixs:
            seller_delta = usdc_deltas.get(SELLER, 0)
            if seller_delta > 0:
                events.append({'type':'sale','time':ts,'usdc':seller_delta,'tx':s['signature'][:44],'wallet':SELLER})
    return counts, events, decoded

def main():
    now_iso = datetime.now(timezone.utc).isoformat()
    
    # Load history for cumulative events
    history = {'acquisitions':[], 'sales':[]}
    if os.path.exists(HISTORY_FILE):
        try:
            history = json.load(open(HISTORY_FILE))
        except Exception:
            pass
    
    # Bot stats
    bot_sigs = get_recent_txns(BOT, 100)
    seller_sigs = get_recent_txns(SELLER, 100)
    
    bot_counts, bot_events, bot_decoded = analyze_txns(BOT, bot_sigs, max_decode=12)
    seller_counts, seller_events, seller_decoded = analyze_txns(SELLER, seller_sigs, max_decode=12)
    
    # txn rates
    def rate(sigs):
        times = [s.get('blockTime') for s in sigs if s.get('blockTime')]
        if len(times) < 2: return None
        span_h = (max(times)-min(times))/3600
        if span_h <= 0: return None
        return round(len(times)/span_h*24)
    
    bot_rate = rate(bot_sigs)
    seller_rate = rate(seller_sigs)
    
    bot_accts, bot_holdings = get_token_account_count(BOT)
    seller_accts, seller_holdings = get_token_account_count(SELLER)
    
    # Balances
    bot_sol = get_sol_balance(BOT)
    seller_sol = get_sol_balance(SELLER)
    bot_usdc = get_usdc_balance(BOT)
    seller_usdc = get_usdc_balance(SELLER)
    
    # Merge new events into history (dedupe by tx sig)
    def merge_events(target, new_events, event_type):
        seen = {e.get('tx') for e in target}
        for ev in new_events:
            if ev['type'] == event_type and ev.get('tx') not in seen:
                target.append({'time': ev['time'], 'usdc_paid' if event_type=='acquisition' else 'usdc_received': round(ev['usdc'],2), 'tx': ev['tx']})
                seen.add(ev.get('tx'))
        # keep most recent 200
        return target[-200:]
    
    history['acquisitions'] = merge_events(history['acquisitions'], bot_events, 'acquisition')
    history['sales'] = merge_events(history['sales'], seller_events, 'sale')
    
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(history, open(HISTORY_FILE,'w'), indent=1)
    
    # Instruction mix strings
    def mix_str(counts):
        if not counts: return '—'
        total = sum(counts.values())
        parts = [f"{k}:{v}" for k,v in sorted(counts.items(), key=lambda x:-x[1])[:4]]
        return ', '.join(parts)
    
    # Recent windows for display (last 25)
    recent_acqs = [{'time':e['time'],'usdc_paid':e['usdc_paid'],'tx':e['tx'],'asset':''} for e in history['acquisitions'][-25:]]
    recent_acqs.reverse()
    recent_sales = [{'time':e['time'],'usdc_received':e['usdc_received'],'tx':e['tx'],'asset':'','buyer':''} for e in history['sales'][-25:]]
    recent_sales.reverse()
    
    data = {
        'generated': now_iso,
        'summary': {
            'bot_txns_per_day': bot_rate,
            'bot_window': f'{len(bot_sigs)} txns sampled',
            'bot_offers_out': bot_accts,
            'bot_holdings': bot_holdings,
            'seller_usdc_balance': seller_usdc,
            'seller_txns_per_day': seller_rate,
            'seller_listing_updates': seller_counts.get('UpdateListing', 0),
        },
        'acquisitions': recent_acqs,
        'sales': recent_sales,
        'profile': {
            'bot_instruction_mix': mix_str(bot_counts),
            'seller_instruction_mix': mix_str(seller_counts),
            'bot_sol_balance': bot_sol,
            'seller_sol_balance': seller_sol,
            'bot_usdc_balance': bot_usdc,
            'seller_usdc_balance': seller_usdc,
            'bot_token_accounts': bot_accts,
            'seller_token_accounts': seller_accts,
        },
    }
    
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=1)
    
    print(f"Dashboard data generated: {DATA_FILE}")
    print(f"  Acquisitions in history: {len(history['acquisitions'])}")
    print(f"  Sales in history: {len(history['sales'])}")
    print(f"  Bot USDC: {bot_usdc:.2f}, Seller USDC: {seller_usdc:.2f}")

if __name__ == '__main__':
    main()