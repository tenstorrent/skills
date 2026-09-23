"""CLI for preparing and running a benchmark subset."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--tasks', default='mmlu_pro,gsm8k_cot,ifeval')
    prepare.add_argument('--counts', default='280,256,256')
    prepare.add_argument('--output', type=Path, required=True)
    prepare.add_argument('--reuse-manifest', type=Path, help='preserve questions/counts while selecting an upstream recipe variant')
    evaluate = commands.add_parser('evaluate')
    evaluate.add_argument('--model', required=True)
    evaluate.add_argument('--base-url', default='http://127.0.0.1:8000')
    evaluate.add_argument('--manifest', type=Path, required=True)
    evaluate.add_argument('--task', required=True)
    evaluate.add_argument('--shared', action='store_true', help='evaluate comma-separated groups in one request pool')
    evaluate.add_argument('--output', type=Path, required=True)
    evaluate.add_argument('--generation', type=json.loads, default={})
    run_parser = commands.add_parser('run')
    run_parser.add_argument('--config', type=Path, required=True)
    run_parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'prepare':
        from benchmark_stage.subsets import prepare as freeze
        tasks = args.tasks.split(',')
        counts = list(map(int, args.counts.split(',')))
        if len(tasks) != len(counts) or len(set(tasks)) != len(tasks):
            parser.error('tasks and counts must have matching lengths and unique task names')
        print(json.dumps(freeze(tasks, dict(zip(tasks, counts)), args.output, args.reuse_manifest), indent=2))
    elif args.command == 'run':
        from benchmark_stage.run import run
        print(json.dumps(run(config_path=args.config, output=args.output), indent=2))
    else:
        from benchmark_stage.evaluate import evaluate, evaluate_groups
        kwargs = dict(model=args.model, base_url=args.base_url, manifest_path=args.manifest,
                      output=args.output, generation=args.generation)
        if args.shared:
            result = evaluate_groups(groups=args.task.split(','), **kwargs)
            print(json.dumps({k: v['benchmark_stage'] for k, v in result.items()}, indent=2))
        else:
            print(json.dumps(evaluate(group=args.task, **kwargs)['benchmark_stage'], indent=2))


if __name__ == '__main__':
    main()
