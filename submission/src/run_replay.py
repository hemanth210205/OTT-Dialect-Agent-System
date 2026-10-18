"""Single entrypoint: replay -> simulator -> outcome judging -> impact, latency and final reports.

    python -m src.run_replay                       # auto-detects the pack next to / above submission/
    python -m src.run_replay --pack ../one_user_one_agent_pack --seed oua-practice

No API key, network or third-party package is needed.
"""
import argparse
import json
import os
import subprocess
import sys
import time

from .data_loader import Pack
from .impact_reporter import (latency_report, pilot_reanalysis, replay_impact, write_final_report,
                              write_impact_report)
from .outcome_judge import judge, load_outcome_events, write_outcomes
from .replay_harness import ReplayHarness

HERE = os.path.dirname(os.path.abspath(__file__))
SUBMISSION = os.path.dirname(HERE)


def find_pack(explicit=None):
    cands = [explicit] if explicit else [
        os.getcwd(), os.path.join(SUBMISSION, '..', 'one_user_one_agent_pack'), os.path.join(SUBMISSION, '..'),
        os.path.join(os.getcwd(), 'one_user_one_agent_pack'), os.path.join(os.getcwd(), '..', 'one_user_one_agent_pack')]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, 'replay', 'triggers.jsonl')):
            return os.path.abspath(c)
    sys.exit('data pack not found; pass --pack <path to one_user_one_agent_pack>')


def run_simulator(pack_dir, out, sim_dir, seed):
    subprocess.run([sys.executable, os.path.join(pack_dir, 'simulator', 'simulate_outcomes.py'), '--pack', pack_dir,
                    '--actions', os.path.join(out, 'actions.jsonl'), '--out', sim_dir, '--seed', seed], check=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pack', help='root of the data pack (contains data/, config/, replay/, simulator/)')
    ap.add_argument('--out', default=os.path.join(SUBMISSION, 'sample_run'), help='output directory')
    ap.add_argument('--seed', default='oua-practice', help='simulator seed')
    ap.add_argument('--skip-sim', action='store_true', help='only run the agent replay')
    ap.add_argument('--robustness-seeds', default='seed-b,seed-c,seed-d',
                    help='comma-separated extra simulator seeds for the sensitivity check ("" to skip)')
    a = ap.parse_args(argv)
    pack_dir = find_pack(a.pack)
    out = os.path.abspath(a.out)
    t0 = time.time()
    print('[1/5] loading pack %s' % pack_dir, file=sys.stderr)
    pack = Pack(pack_dir)
    print('[2/5] replaying %d triggers, %d live events, %d control events' % (
        len(pack.triggers), len(pack.live_events), len(pack.control_events)), file=sys.stderr)
    h = ReplayHarness(pack).run()
    h.write(out)
    print('      %d decisions, %d actions' % (len(h.decisions), len(h.actions)), file=sys.stderr)
    with open(os.path.join(out, 'latency_report.json'), 'w', encoding='utf-8') as f:
        json.dump(latency_report(h.decisions, pack.control_events), f, indent=2)
    if a.skip_sim:
        return h
    sim_dir = os.path.join(out, 'sim')
    print('[3/5] running simulator (seed %s)' % a.seed, file=sys.stderr)
    run_simulator(pack_dir, out, sim_dir, a.seed)
    print('[4/5] judging goals', file=sys.stderr)
    outcomes = judge(h.store, pack, os.path.join(sim_dir, 'outcome_events.jsonl'))
    write_outcomes(outcomes, out)
    h.store.save(os.path.join(out, 'viewer_state'))
    print('[5/5] writing reports', file=sys.stderr)
    with open(os.path.join(sim_dir, 'simulator_report.json'), encoding='utf-8') as f:
        sim_report = json.load(f)
    events = load_outcome_events(os.path.join(sim_dir, 'outcome_events.jsonl'))
    imp = replay_impact(pack, h.store, h.decisions, outcomes, events, sim_report)
    pilot = pilot_reanalysis(pack)
    write_impact_report(os.path.join(out, 'impact_report.md'), imp, pilot, h.planner)
    with open(os.path.join(out, 'impact_summary.json'), 'w', encoding='utf-8') as f:
        json.dump({'replay': imp, 'pilot': pilot}, f, indent=2, default=str)
    with open(os.path.join(out, 'latency_report.json'), encoding='utf-8') as f:
        lat = json.load(f)
    seeds = [(a.seed, imp['primary']['result'])]
    for s in [x for x in a.robustness_seeds.split(',') if x]:
        print('      seed sensitivity: simulator seed %s' % s, file=sys.stderr)
        sdir = os.path.join(out, 'sim_seeds', s)
        run_simulator(pack_dir, out, sdir, s)
        so = judge(h.store, pack, os.path.join(sdir, 'outcome_events.jsonl'), record_learnings=False)
        with open(os.path.join(sdir, 'simulator_report.json'), encoding='utf-8') as f:
            srep = json.load(f)
        si = replay_impact(pack, h.store, h.decisions, so, load_outcome_events(os.path.join(sdir, 'outcome_events.jsonl')),
                           srep)
        seeds.append((s, si['primary']['result']))
    write_final_report(os.path.join(out, 'final_report.md'), pack, h, outcomes, imp, pilot, lat, seeds)
    print('done in %.1fs -> %s' % (time.time() - t0, out), file=sys.stderr)
    return h


if __name__ == '__main__':
    main()
