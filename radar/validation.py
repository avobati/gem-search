"""Forward-only labels and matched-universe baseline measurements."""
from collections import defaultdict
import statistics
import time
from radar.engine import HORIZONS, primary


def label_returns(store, now=None):
    now = time.time() if now is None else now
    existing = {r['id'] for r in store.history('returns', cutoff=now, limit=1000000, fields=('decision_id',))}
    histories = {}
    for decision in store.history('decisions', cutoff=now, limit=100000, fields=('features','cutoff','run_id')):
        base = decision['features'].get('price')
        if not base or decision['features'].get('stale'):
            continue
        pending = [(h,s) for h,s in HORIZONS.items() if decision['id']+':'+h not in existing
                   and now >= decision['cutoff']+s+max(300,min(1800,s*.05))]
        if not pending:
            continue
        if decision['mint'] not in histories:
            histories[decision['mint']] = store.history('observations', decision['mint'], now, limit=100000,
                                                       fields=('price','pool','provider','liquidity'))
        history = histories[decision['mint']]
        # Follow the same pool/provider as the decision to avoid switching artifacts.
        f = decision['features']
        observations = sorted([r for r in history if r['available_at'] >= decision['cutoff']
                               and r.get('price') and r.get('pool') == f.get('pool')
                               and r.get('provider') == f.get('source')], key=lambda r:r['available_at'])
        for horizon, seconds in pending:
            target = decision['cutoff'] + seconds
            tolerance = max(300, min(1800, seconds*.05))
            if now < target+tolerance:
                continue
            identity = decision['id']+':'+horizon
            eligible = [r for r in observations if target <= r['available_at'] <= target+tolerance]
            path = [r for r in observations if r['available_at'] <= target]
            observation = eligible[0] if eligible else None
            if observation:
                path.append(observation)
            prices = [base] + [r['price'] for r in path]
            peak, drawdown = base, 0
            for price in prices:
                peak = max(peak, price); drawdown = min(drawdown, price/peak-1)
            peak_row = max(path, key=lambda r:r['price'], default=None)
            label = {'decision_id': decision['id'], 'run_id': decision['run_id'], 'horizon': horizon,
                     'target_at': target, 'observed_at': observation['available_at'] if observation else None,
                     'status': 'observed' if observation else 'missing',
                     'return_pct': (observation['price']/base-1)*100 if observation else None,
                     'max_return_pct': (max(prices)/base-1)*100 if path else None,
                     'min_return_pct': (min(prices)/base-1)*100 if path else None,
                     'max_drawdown_pct': drawdown*100 if path else None,
                     'time_to_peak_seconds': peak_row['available_at']-decision['cutoff'] if peak_row and peak_row['price']>base else 0,
                     'sample_count': len(path), 'sampling': 'sampled prices; intrainterval extremes and executable exits unknown',
                     'exit_liquidity': observation.get('liquidity') if observation else None}
            store.append('returns', decision['mint'], label, now, identity=identity)


def metrics(decisions, labels, method, horizon, threshold, top=20):
    mature = [d for d in decisions if d['id'] in labels and labels[d['id']]['status'] == 'observed']
    winners = [d for d in mature if labels[d['id']]['return_pct'] >= threshold]
    # Gem is risk-gated, baselines expose their selected risk as well.
    selected_all = [d for d in decisions if d['ranks'][method] <= top and (method!='gem' or d['score']['candidate'])]
    mature_ids = {d['id'] for d in mature}
    selected = [d for d in selected_all if d['id'] in mature_ids]
    values = [labels[d['id']]['return_pct'] for d in selected]
    hits = sum(labels[d['id']]['return_pct'] >= threshold for d in selected)
    false_positives = len(selected)-hits
    negatives = len(mature)-len(winners)
    return {'method': method, 'horizon': horizon, 'threshold': threshold, 'top': top,
            'selected': len(selected_all), 'observed': len(selected), 'missing_or_unmatured': len(selected_all)-len(selected),
            'hit_rate': hits/len(selected) if selected else None,
            'precision': hits/len(selected) if selected else None,
            'recall_observed_universe': hits/len(winners) if winners else None,
            'false_positive_rate': false_positives/negatives if negatives else None,
            'false_discovery_rate': false_positives/len(selected) if selected else None,
            'median_return_pct': statistics.median(values) if values else None,
            'average_return_pct': statistics.mean(values) if values else None,
            'median_max_return_pct': statistics.median([labels[d['id']]['max_return_pct'] for d in selected if labels[d['id']]['max_return_pct'] is not None]) if selected and any(labels[d['id']]['max_return_pct'] is not None for d in selected) else None,
            'max_drawdown_pct': min([labels[d['id']]['max_drawdown_pct'] for d in selected if labels[d['id']]['max_drawdown_pct'] is not None], default=None),
            'rug_avoidance_rate': None, 'note': 'Correlated repeated cohorts; observed-universe recall only. Missing exits are excluded and disclosed.'}


def report(store):
    decisions = store.history('decisions', limit=100000, fields=('ranks','score'))
    labels = store.history('returns', limit=100000)
    rows = []
    for horizon in ('6h','12h','24h','3d','7d'):
        by_id = {r['decision_id']:r for r in labels if r['horizon']==horizon}
        for threshold in (20,50,100,200,500):
            for method in ('gem','random','volume','gainer','momentum','liquidity'):
                rows.append(metrics(decisions, by_id, method, horizon, threshold))
    return {'generated_at': time.time(), 'decision_count': len(decisions), 'label_count': len(labels), 'metrics': rows,
            'backtest': {'status': 'not_available', 'reason': 'No preexisting timestamp-bound token history; forward evidence must accumulate'},
            'out_of_sample': {'status': 'not_established', 'reason': 'No independent mature training/validation/test cohorts yet'},
            'forward_testing': {'status': 'collecting' if decisions else 'not_started'},
            'next_experiments': ['Compare frozen Gem cohorts with matched-universe baselines',
                                 'Collect transaction-level wallet history before classifying smart wallets',
                                 'Evaluate missing-exit worst-case sensitivity', 'Calibrate horizon models on disjoint walk-forward periods']}


def walk_forward_periods(start, end, train=30*86400, validation=7*86400, test=7*86400, embargo=7*86400):
    """Embargo covers maximum label horizon; no train label crosses into validation."""
    periods = []
    cursor = start
    while cursor+train+validation+test+2*embargo <= end:
        train_end = cursor+train
        validation_start = train_end+embargo
        test_start = validation_start+validation+embargo
        periods.append({'train': [cursor,train_end], 'validation': [validation_start,validation_start+validation],
                        'test': [test_start,test_start+test], 'max_label_horizon': embargo})
        cursor += test
    return periods


def evaluate_walk_forward(store, periods, weight_options, horizon='24h', threshold=100, minimum=30):
    """Evaluate alternative rule weights on frozen features; never deploy them.

    Outcomes must be available before each selection boundary. Final test labels
    are read only after weights are selected using training and validation.
    """
    from radar.engine import score_token
    if not periods:
        return {'folds':[], 'horizon':horizon, 'threshold':threshold, 'production_weights_changed':False,
                'caveat':'No mature chronological folds yet'}
    decisions=store.history('decisions',limit=100000)
    labels={r['decision_id']:r for r in store.history('returns',limit=100000)
            if r['horizon']==horizon and r['status']=='observed'}
    def evaluate(interval, weights):
        start,end=interval
        rows=[d for d in decisions if start<=d['cutoff']<end and d['id'] in labels
              and labels[d['id']]['available_at']<=end and labels[d['id']]['target_at']<=end]
        cohorts=defaultdict(list)
        for d in rows:
            s=score_token(d['features'],d['risk'],d['research'],weights)
            if s['candidate']:
                cohorts[d['run_id']].append((s['total'],d))
        # One initial recommendation per mint per period avoids treating each
        # five-minute repeat as an independent sample.
        picked={}
        for run_id in sorted(cohorts):
            for _,d in sorted(cohorts[run_id],key=lambda x:x[0],reverse=True)[:20]:
                picked.setdefault(d['mint'],d)
        outcomes=[labels[d['id']]['return_pct'] for d in picked.values()]
        return {'count':len(outcomes),'hit_rate':sum(v>=threshold for v in outcomes)/len(outcomes) if outcomes else None,
                'median_return_pct':statistics.median(outcomes) if outcomes else None}
    folds=[]
    for period in periods:
        train=[]
        for weights in weight_options:
            result=evaluate(period['train'],weights)
            if result['count']>=minimum:
                train.append((weights,result))
        # Weight options predeclared before testing, then selected on validation.
        choices=[]
        for weights,training in train:
            validation=evaluate(period['validation'],weights)
            if validation['count']>=minimum:
                choices.append((weights,training,validation))
        if not choices:
            folds.append({'period':period,'status':'insufficient_independent_mature_tokens'})
            continue
        selected=max(choices,key=lambda x:(x[2]['hit_rate'],x[2]['median_return_pct']))
        folds.append({'period':period,'status':'evaluated','weights':selected[0],
                      'training':selected[1],'validation':selected[2],'test':evaluate(period['test'],selected[0])})
    return {'folds':folds,'horizon':horizon,'threshold':threshold,'production_weights_changed':False,
            'caveat':'Observed-universe experiment; predeclare weight grid and assess multiple-testing bias'}
