"""Launch supercloud experiments defined by config files.

Usage example:     python scripts/supercloud/launch.py --config
example.yaml --user tslvr
"""

import argparse
import sys

from predicators.scripts.cluster_utils import generate_run_configs, \
    parse_config, run_cmds_on_machine
from predicators.scripts.supercloud.submit_supercloud_job import \
    submit_supercloud_job
from predicators.src.settings import CFG

SUPERCLOUD_IP = "txe1-login.mit.edu"


def _main() -> None:
    # Set up argparse.
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=str)
    parser.add_argument("--user", required=True, type=str)
    # This flag is used internally by the script.
    parser.add_argument("--on_supercloud", action="store_true")
    args = parser.parse_args()
    # If we're not yet on supercloud, ssh in and prepare. Then, we will
    # run this file again, but with the --on_supercloud flag.
    if not args.on_supercloud:
        return _launch_from_local(args.config, args.user)
    # If we're already on supercloud, launch the experiments.
    return _launch_experiments(args.config)


def _launch_from_local(config_file: str, user: str) -> None:
    config = parse_config(config_file)
    branch = config["BRANCH"]
    str_args = " ".join(sys.argv)
    server_cmds = [
        # Prepare the predicators directory.
        "predicate",
        "git fetch --all",
        f"git checkout {branch}",
        "git pull",
        # Remove old results.
        "rm -f results/* logs/* saved_approaches/* saved_datasets/*",
        # Run this file again, but with the on_supercloud flag.
        f"python {str_args} --on_supercloud",
    ]
    run_cmds_on_machine(server_cmds, user, SUPERCLOUD_IP)


def _launch_experiments(config_file: str) -> None:
    # Loop over run configs.
    for cfg in generate_run_configs(config_file):
        # Create the args and flags string.
        arg_str = " ".join(f"--{a}" for a in cfg.args)
        flag_str = " ".join(f"--{f} {v}" for f, v in cfg.flags.items())
        args_and_flags_str = (f"--env {cfg.env} "
                              f"--approach {cfg.approach} "
                              f"--experiment_id {cfg.experiment_id} "
                              f"{arg_str} "
                              f"{flag_str}")
        # Create the log dir.
        if "log_dir" in cfg.flags:
            log_dir = "log_dir"
        else:
            log_dir = CFG.log_dir
        logfile_prefix = f"{cfg.env}__{cfg.approach}__{cfg.experiment_id}"
        # Launch a job for this experiment.
        submit_supercloud_job(cfg.experiment_id, log_dir, logfile_prefix,
                              args_and_flags_str, cfg.start_seed, cfg.num_seed)


if __name__ == "__main__":
    _main()
