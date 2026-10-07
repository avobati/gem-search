"""Export honest research results and optional walk-forward rule experiments."""
import argparse
import json
from pathlib import Path
from radar.engine import WEIGHTS
from radar.store import Store
from radar.validation import report, walk_forward_periods, evaluate_walk_forward


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='data/radar-report.json')
    args=parser.parse_args()
    store=Store()
    result=report(store)
    decisions=store.history('decisions',limit=100000)
    periods=walk_forward_periods(min((r['cutoff'] for r in decisions),default=0),
                                 max((r['cutoff'] for r in decisions),default=0))
    options=[WEIGHTS,dict(WEIGHTS,momentum=30,liquidity=10),dict(WEIGHTS,momentum=20,project=15)]
    result['walk_forward']=evaluate_walk_forward(store,periods,options)
    destination=Path(args.output);destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(result,indent=2,allow_nan=False),encoding='utf-8')
    print(f'Research report saved to {destination}')


if __name__=='__main__':
    main()
