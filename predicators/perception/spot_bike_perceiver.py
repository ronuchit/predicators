"""A perceiver specific to the spot bike env."""

from typing import Optional, Set

from predicators.envs import BaseEnv, get_or_create_env
from predicators.envs.spot_env import SpotBikeEnv, _PartialPerceptionState
from predicators.perception.base_perceiver import BasePerceiver
from predicators.settings import CFG
from predicators.spot_utils.spot_utils import obj_name_to_apriltag_id
from predicators.structs import Action, EnvironmentTask, Object, Observation, \
    State, Task

# Each observation is a tuple of four 2D boolean masks (numpy arrays).
# The order is: free, goals, boxes, player.


class SpotBikePerceiver(BasePerceiver):
    """A spot-bike-env-specific perceiver."""

    def __init__(self) -> None:
        super().__init__()
        self._prev_action: Optional[Action] = None
        self._curr_task_objects: Set[Object] = set()
        assert CFG.env == "spot_bike_env"
        self._curr_env: Optional[BaseEnv] = None

    @classmethod
    def get_name(cls) -> str:
        return "spot_bike_env"

    def reset(self, env_task: EnvironmentTask) -> Task:
        self._prev_action = None
        self._curr_task_objects = set(env_task.init.data)
        # We currently have hardcoded logic that expects
        # certain items to be in the state; we can generalize
        # this later.
        assert set(["hex_key", "hammer", "hex_screwdriver",
                    "brush"]).issubset(obj.name
                                       for obj in self._curr_task_objects)
        self._curr_env = get_or_create_env("spot_bike_env")
        assert isinstance(self._curr_env, SpotBikeEnv)
        return env_task.task

    def update_perceiver_with_action(self, action: Action) -> None:
        self._prev_action = action

    def step(self, observation: Observation) -> State:
        assert isinstance(observation, _PartialPerceptionState)
        if self._prev_action is not None:
            assert self._curr_env is not None and isinstance(
                self._curr_env, SpotBikeEnv)
            controller_name, objects, _ = self._curr_env.parse_action(
                observation, self._prev_action)
            if "grasp" in controller_name.lower():
                # We know that the object that we attempted to grasp was
                # the second argument to the controller.
                object_attempted_to_grasp = objects[1].name
                grasp_obj_id = obj_name_to_apriltag_id[
                    object_attempted_to_grasp]
                robot_object = objects[0]
                observation.set(robot_object, "curr_held_item_id",
                                grasp_obj_id)
                self._curr_env.update_observation(observation)
            elif "place" in controller_name.lower():
                robot_object = objects[0]
                observation.set(robot_object, "curr_held_item_id", 0.0)
                self._curr_env.update_observation(observation)
        return observation
