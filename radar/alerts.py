"""Optional configured Telegram delivery. Persist before send; uncertainty holds."""
import json
import os
import time
from urllib.request import Request, urlopen


def notify(store, decisions, transport=None):
    token, chat = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if os.getenv('TELEGRAM_ALERTS_ENABLED') != '1' or not token or not chat:
        return 0
    status = store.status()
    if time.time()-status.get('alerts',{}).get('updated',0)<900:
        return 0
    candidates = [r for r in decisions if r['score']['candidate'] and r['score']['total']>=80 and r['risk']['score']<=25 and r['score']['coverage']>=75]
    candidates.sort(key=lambda r:r['score']['total'],reverse=True)
    def send(body):
        request=Request('https://api.telegram.org/bot'+token+'/sendMessage',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=15) as response:
            result=json.loads(response.read(65536))
            if not result.get('ok'):
                raise ValueError('Delivery rejected')
    for row in candidates:
        key='alert:'+row['mint']
        if time.time()-status.get(key,{}).get('updated',0)<86400:
            continue
        f,s,r=row['features'],row['score'],row['risk']
        text=('💎 SOLANA GEM RESEARCH LEAD\n'+str(f.get('name') or row['mint'])+'\n'
              f'Gem {s["total"]}/100 · Risk {r["score"]}/100\nLiquidity ${f["liquidity"]:,.0f}\n'
              +'WHY:\n'+'\n'.join(row['explanation']['why'][:3])+'\nRISKS:\n'+'\n'.join(row['explanation']['risks'][:2])
              +'\nCA: '+row['mint']+'\nUncalibrated research score. No automatic purchase.')
        # Unknown send outcome never triggers an immediate retry.
        store.set_status(key,{'state':'dispatching'});store.set_status('alerts',{'state':'dispatching'})
        try:
            (transport or send)({'chat_id':chat,'text':text[:4000],'disable_web_page_preview':True})
            store.set_status(key,{'state':'sent'})
            return 1
        except Exception:
            store.set_status(key,{'state':'unknown'})
            return 0
    return 0
