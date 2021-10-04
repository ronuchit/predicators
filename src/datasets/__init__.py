"""Create offline datasets.
"""
from typing import List
from predicators.src.envs import BaseEnv
from predicators.src.structs import Dataset, Task
from predicators.src.datasets.demo_only import create_demo_data
from predicators.src.datasets.demo_replay import create_demo_replay_data


def create_dataset(env: BaseEnv, train_tasks: List[Task],
                   data_config: dict) -> Dataset:
    """Create offline datasets.
    """
    if data_config["method"] == "demo":
        return create_demo_data(env, train_tasks, data_config)
    if data_config["method"] == "demo+replay":
        return create_demo_replay_data(env, train_tasks, data_config)
    raise NotImplementedError("Unrecognized dataset method.")
