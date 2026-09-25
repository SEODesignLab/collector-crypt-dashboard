#!/usr/bin/env python3
"""
Collector Crypt slab-bot monitor — zero-LLM, pure Python.
Runs every 20m via cron (no_agent). Does:
  1. On-chain analysis via Alchemy RPC (acquisitions, sales, balances)
  2. Merges into cumulative append-only history (never truncated)
  3. Pushes dashboard data to GitHub Pages
  4. Appends one-line entry to Obsidian daily log
  5. Prints to stdout ONLY if notable (stdout -> Telegram via cron delivery).
     Prints nothing = silent = no Telegram spam.
"""
import requests, json, os, sys, subprocess, base64, re
from datetime import datetime, timezone

RPC = 'https://solana-mainnet.g.alchemy.com/v2/WsIAMnMfQS4V1SdWpHS7o'
USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
BOT = 'FrY8u2MhPoV3xjSxeZf74ftPMdvthAo9or6fLGwLAXr8'
SELLER = 'Dpua5doi7EKeh9oSEpCLe99o76eFFC5FrrartM95wQBQ'
DASH_DIR = '/root/collector-crypt-dashboard'
DATA_DIR = os.path.join(DASH_DIR, 'data')
DATA_FILE = os.path.join(DATA_DIR, 'dashboard-data.json')
HISTORY_FILE = os.path.join(DATA_DIR, 'history.json')
OBSIDIAN_LOG = '/root/Obsidian/forge/Agent-Hermes/research/collector-crypt-slab-bot/2026-09-24-slab-bot-analysis.md'

# Alert thresholds
SALE_ALERT_USDC = 50.0
BALANCE_MOVE_ALERT = 100.0

def rpc_call(method, params, retries=2):
    for attempt in range(retries):
        try:
            r = requests.post(RPC, json={'jsonrpc':'2.0','id':1,'method':method,'params':params}, timeout=20)
            return r.json().get('result')
        except Exception:
            if attempt == retries-1:
                return None
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
    if isinstance(res, dict): res = res.get('value', 0)
    return (res or 0) / 1e9

def get_token_account_count(wallet):
    res = rpc_call('getTokenAccountsByOwner', [wallet, {'programId':'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'}, {'encoding':'jsonParsed'}])
    if not res: return 0, 0
    accounts = res.get('value', [])
    nonzero = sum(1 for a in accounts if (a.get('account',{}).get('data',{}).get('parsed',{}).get('info',{}).get('tokenAmount',{}).get('uiAmount',0) or 0) > 0)
    return len(accounts), nonzero

def get_recent_txns(wallet, limit=100):
    res = rpc_call('getSignaturesForAddress', [wallet, {'limit': limit}])
    return [s for s in (res or []) if not s.get('err')]

def get_card_info_from_tx(tx, keys):
    """Extract card metadata (name, arweave URI, image) from a CC instruction's
    position-[6] metadata escrow account. Returns dict or None."""
    try:
        for ix in tx['transaction']['message']['instructions']:
            if not isinstance(ix, dict): continue
            p = ix.get('programId')
            pid = keys[p] if isinstance(p, int) and p < len(keys) else str(p)
            if 'CcmRKTu' not in pid: continue
            accts = ix.get('accounts', [])
            if len(accts) < 7: continue
            meta_key = keys[accts[6]] if isinstance(accts[6], int) and accts[6] < len(keys) else accts[6]
            r = requests.post(RPC, json={'jsonrpc':'2.0','id':9,'method':'getAccountInfo','params':[meta_key,{'encoding':'base64'}]}, timeout=15)
            info = (r.json().get('result') or {}).get('value',{})
            data = base64.b64decode(info['data'][0]) if info.get('data') else b''
            # find arweave URI + name strings
            uris = re.findall(rb'https://arweave\.net/[A-Za-z0-9_-]{20,}', data)
            names = re.findall(rb'[\x20-\x7e]{8,90}', data)
            card = {'meta_escrow': meta_key}
            if uris:
                card['meta_uri'] = uris[0].decode()
            # name is usually the first long printable string that's not a URL
            for n in names:
                s = n.decode()
                if not s.startswith('http') and len(s) > 10:
                    card['name'] = s.rstrip('\x00?').strip()
                    break
            card = fetch_arweave_meta(card)
            return card
    except Exception:
        return None
    return None

def fetch_arweave_meta(card):
    """Enrich card dict with name/image/insured value from Arweave metadata JSON."""
    uri = card.get('meta_uri')
    if not uri: return card
    try:
        r = requests.get(uri, timeout=10)
        if r.status_code == 200:
            m = r.json()
            card['full_name'] = m.get('name', card.get('name',''))
            card['image'] = m.get('image','')
            attrs = {a.get('trait_type'): a.get('value') for a in m.get('attributes',[]) if isinstance(a,dict)}
            card['cc_id'] = attrs.get('Collector Crypt ID','')
            card['insured'] = attrs.get('Insured Value','')
            card['grader'] = attrs.get('Grading Company','')
            card['serial'] = attrs.get('Serial Number','')
    except Exception:
        pass
    return card

def analyze_txns(sigs, max_decode=12):
    counts = {}
    events = []
    for s in sigs[:max_decode]:
        tx = rpc_call('getTransaction', [s['signature'], {'maxSupportedTransactionVersion':0,'encoding':'jsonParsed'}])
        if not tx: continue
        tx_keys = []
        for k in tx['transaction']['message'].get('accountKeys', []):
            tx_keys.append(k.get('pubkey','') if isinstance(k, dict) else k)
        loaded = tx.get('meta',{}).get('loadedAddresses',{})
        if loaded:
            tx_keys += loaded.get('writable',[]) + loaded.get('readonly',[])
        logs = tx.get('meta',{}).get('logMessages',[]) or []
        ixs = [l.replace('Program log: Instruction: ','') for l in logs if 'Instruction:' in l]
        for ix in ixs:
            counts[ix] = counts.get(ix,0) + 1
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
            card = get_card_info_from_tx(tx, tx_keys) or {}
            events.append({'type':'acquisition','time':ts,'usdc':abs(usdc_deltas.get(BOT,0)),'tx':s['signature'],
                           'card': card.get('name',''), 'image': card.get('image',''), 'cc_id': card.get('cc_id',''), 'insured': card.get('insured','')})
        if 'BuyCore' in ixs and usdc_deltas.get(SELLER,0) > 0:
            card = get_card_info_from_tx(tx, tx_keys) or {}
            events.append({'type':'sale','time':ts,'usdc':usdc_deltas[SELLER],'tx':s['signature'],
                           'card': card.get('name',''), 'image': card.get('image',''), 'cc_id': card.get('cc_id',''), 'insured': card.get('insured','')})
    return counts, events

def rate(sigs):
    times = [s.get('blockTime') for s in sigs if s.get('blockTime')]
    if len(times) < 2: return None
    span_h = (max(times)-min(times))/3600
    return round(len(times)/span_h*24) if span_h > 0 else None

def mix_str(counts):
    if not counts: return '—'
    return ', '.join(f"{k}:{v}" for k,v in sorted(counts.items(), key=lambda x:-x[1])[:4])

def merge_events(target, new_events, event_type, seen):
    added = 0
    for ev in new_events:
        if ev['type'] == event_type and ev.get('tx') not in seen:
            key = 'usdc_paid' if event_type=='acquisition' else 'usdc_received'
            # enrich card info from Arweave (once per new event)
            card = {'meta_escrow': '', 'meta_uri': '', 'name': ev.get('card','')}
            # find the full event dict with meta_uri
            card_full = {k: v for k, v in ev.items() if k in ('card','image','cc_id','insured')}
            target.append({'time': ev['time'], key: round(ev['usdc'],2), 'tx': ev['tx'], **card_full})
            seen.add(ev['tx'])
            added += 1
    return added

def main():
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    alerts = []

    # Load history
    history = {'acquisitions':[], 'sales':[], 'balance_history':[]}
    if os.path.exists(HISTORY_FILE):
        try: history = json.load(open(HISTORY_FILE))
        except Exception: pass
    for k in ['acquisitions','sales','balance_history']:
        if k not in history: history[k] = []
    prev_seller_usdc = history['balance_history'][-1]['seller_usdc'] if history['balance_history'] else None

    # On-chain analysis
    bot_sigs = get_recent_txns(BOT, 100)
    seller_sigs = get_recent_txns(SELLER, 100)
    if not bot_sigs and not seller_sigs:
        # Both wallets silent — check how long
        print("⚠️ Both bot wallets returned no recent transactions — possible RPC issue or wallets went silent.")
        return
    bot_counts, bot_events = analyze_txns(bot_sigs)
    seller_counts, seller_events = analyze_txns(seller_sigs)

    seen_acq = {e.get('tx') for e in history['acquisitions']}
    seen_sale = {e.get('tx') for e in history['sales']}
    new_acqs = merge_events(history['acquisitions'], bot_events, 'acquisition', seen_acq)
    new_sales = merge_events(history['sales'], seller_events, 'sale', seen_sale)

    # Balances & stats
    bot_accts, bot_holdings = get_token_account_count(BOT)
    seller_accts, _ = get_token_account_count(SELLER)
    bot_sol = get_sol_balance(BOT)
    seller_sol = get_sol_balance(SELLER)
    bot_usdc = get_usdc_balance(BOT)
    seller_usdc = get_usdc_balance(SELLER)

    # Balance history snapshot
    history['balance_history'].append({'ts': now_iso, 'seller_usdc': round(seller_usdc,2), 'bot_sol': round(bot_sol,4), 'seller_sol': round(seller_sol,4)})
    history['balance_history'] = history['balance_history'][-10000:]

    # Alerts
    if new_acqs > 0:
        for e in history['acquisitions'][-new_acqs:]:
            alerts.append(f"🤖 Bot acquisition: paid ${e['usdc_paid']:.2f} USDC at {e['time']} — https://solscan.io/tx/{e['tx']}")
    if new_sales > 0:
        for e in history['sales'][-new_sales:]:
            if e['usdc_received'] >= SALE_ALERT_USDC:
                alerts.append(f"💰 Seller sale: received ${e['usdc_received']:.2f} USDC at {e['time']} — https://solscan.io/tx/{e['tx']}")
    if prev_seller_usdc is not None and abs(seller_usdc - prev_seller_usdc) >= BALANCE_MOVE_ALERT:
        direction = 'up' if seller_usdc > prev_seller_usdc else 'down'
        alerts.append(f"📊 Seller USDC moved ${abs(seller_usdc-prev_seller_usdc):.2f} {direction}: now ${seller_usdc:.2f}")

    # Write history
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(history, open(HISTORY_FILE,'w'), indent=1)

    # Build dashboard data
    recent_acqs = [{'time':e['time'],'usdc_paid':e['usdc_paid'],'tx':e['tx'],'card':e.get('card',''),'image':e.get('image',''),'cc_id':e.get('cc_id',''),'insured':e.get('insured','')} for e in reversed(history['acquisitions'])]
    recent_sales = [{'time':e['time'],'usdc_received':e['usdc_received'],'tx':e['tx'],'card':e.get('card',''),'image':e.get('image',''),'cc_id':e.get('cc_id',''),'insured':e.get('insured',''),'buyer':''} for e in reversed(history['sales'])]
    total_acq = sum(e['usdc_paid'] for e in history['acquisitions'])
    total_sale = sum(e['usdc_received'] for e in history['sales'])

    data = {
        'generated': now_iso,
        'summary': {
            'bot_txns_per_day': rate(bot_sigs),
            'bot_window': f'{len(bot_sigs)} txns sampled',
            'bot_offers_out': bot_accts,
            'bot_holdings': bot_holdings,
            'seller_usdc_balance': seller_usdc,
            'seller_txns_per_day': rate(seller_sigs),
            'seller_listing_updates': seller_counts.get('UpdateListing', 0),
            'total_acquisitions': len(history['acquisitions']),
            'total_sales': len(history['sales']),
            'total_acq_usdc': round(total_acq,2),
            'total_sale_usdc': round(total_sale,2),
        },
        'balance_history': history['balance_history'],
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
    json.dump(data, open(DATA_FILE,'w'), indent=1)

    # Git push (commit even if only balance_history point changed — chart needs every point)
    try:
        subprocess.run(['git','add','-A'], cwd=DASH_DIR, check=True, capture_output=True)
        subprocess.run(['git','-C',DASH_DIR,'commit','-m',f'data update {now.strftime("%Y-%m-%dT%H:%M")}Z'], cwd=DASH_DIR, check=True, capture_output=True)
        subprocess.run(['git','-C',DASH_DIR,'push','origin','main'], cwd=DASH_DIR, check=True, capture_output=True, timeout=60)
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Dashboard git push failed: {e.stderr.decode()[:200] if e.stderr else 'unknown'}")
    except Exception as e:
        print(f"⚠️ Dashboard push error: {e}")

    # Obsidian one-line log
    try:
        line = f"- {now.strftime('%Y-%m-%d %H:%M')} UTC — acq:{new_acqs} sale:{new_sales} seller_usdc:${seller_usdc:.2f}\n"
        with open(OBSIDIAN_LOG, 'a') as f:
            f.write(line)
        if new_acqs > 0 or new_sales > 0:
            detail = "\n".join(f"  - {'ACQ' if e in history['acquisitions'][-new_acqs:] else 'SALE'}: {e['time']} ${e.get('usdc_paid', e.get('usdc_received',0)):.2f} tx:{e['tx']}" for e in (history['acquisitions'][-new_acqs:] + history['sales'][-new_sales:]))
            with open(OBSIDIAN_LOG, 'a') as f:
                f.write(detail + "\n")
    except Exception as e:
        print(f"⚠️ Obsidian log append failed: {e}")

    # Only print alerts if notable (stdout -> Telegram)
    if alerts:
        print("🎴 Collector Crypt slab-bot alert:\n" + "\n".join(alerts))
    # else: print nothing = silent = no Telegram message

if __name__ == '__main__':
    main()