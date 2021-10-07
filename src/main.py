"""Main entry point for running approaches in environments.

Example usage:
    python src/main.py --env cover --approach oracle --seed 0

Another example usage:
    python src/main.py --env cover --approach oracle --seed 0 \
        --make_videos --num_test_tasks 1
"""

import time
from predicators.src.args import parse_args
from predicators.src.settings import CFG
from predicators.src.envs import create_env
from predicators.src.approaches import create_approach, ApproachTimeout, \
    ApproachFailure
from predicators.src.datasets import create_dataset
from predicators.src import utils


def main() -> None:
    """Main entry point for running approaches in environments.
    """
    start = time.time()
    # Parse & validate args
    args = parse_args()
    utils.update_config(args)
    # Create & seed classes
    env = create_env(CFG.env)
    approach = create_approach(CFG.approach, env.simulate, env.predicates,
                               env.options, env.types, env.action_space,
                               env.get_train_tasks())
    env.seed(CFG.seed)
    approach.seed(CFG.seed)
    # If approach is learning-based, get training datasets
    if approach.is_learning_based:
        dataset = create_dataset(env)
        approach.learn_from_offline_dataset(dataset)
    # Run approach
    test_tasks = env.get_test_tasks()
    for i, task in enumerate(test_tasks):
        try:
            policy = approach.solve(task, timeout=CFG.timeout)
        except (ApproachTimeout, ApproachFailure) as e:
            print(f"Task {i+1} / {len(test_tasks)}: Approach failed to "
                  f"solve with error: {e}")
            continue
        _, video, solved = utils.run_policy_on_task(policy, task,
            env.simulate, env.predicates, CFG.make_videos, env.render)
        if solved:
            print(f"Task {i+1} / {len(test_tasks)}: SOLVED")
        else:
            print(f"Task {i+1} / {len(test_tasks)}: Policy failed")
        if CFG.make_videos:
            outfile = f"{utils.get_config_path_str()}__task{i}.mp4"
            utils.save_video(outfile, video)
    print(f"\n\nMain script terminated in {time.time()-start:.5f} seconds")


if __name__ == "__main__":  # pragma: no cover
    main()
