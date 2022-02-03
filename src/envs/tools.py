"""Tools domain, where the robot must interact with a variety of items
and tools. Items are screws, nails, and bolts. Tools are screwdrivers,
wrenches, and hammers. Screws are fastened using screwdrivers or the
robot's hand. Nails are fastened using hammers. Bolts are fastened using
wrenches.

Screwdrivers have a shape and a size. The shape must match the screw shape,
but is not captured using any given predicate. Some screw shapes can be
fastened using hands directly. The screwdriver's size must be small enough
that it is graspable by the robot, which is captured by a predicate. Hammer
sizes work the same way as screwdriver sizes. Wrench sizes don't matter.
"""

from typing import List, Set, Sequence, Dict, Tuple, Optional, Union, Any
import numpy as np
from gym.spaces import Box
from predicators.src.envs import BaseEnv
from predicators.src.structs import Type, Predicate, State, Task, \
    ParameterizedOption, Object, Action, GroundAtom, Image, Array
from predicators.src.settings import CFG
from predicators.src import utils


class ToolsEnv(BaseEnv):
    """Tools domain."""
    # Parameters that aren't important enough to need to clog up settings.py
    table_lx = -10.0
    table_ly = -10.0
    table_ux = 10.0
    table_uy = 10.0
    contraption_size = 1.0
    close_thresh = 0.1
    # For a screw of a particular shape, if the shape of every graspable
    # screwdriver differs by at least this amount, then this screw is required
    # to be fastened by hand. Otherwise, it is required to be fastened by the
    # graspable screwdriver that has the smallest difference in shape.
    screw_shape_hand_thresh = 0.25

    def __init__(self) -> None:
        super().__init__()
        # Types
        self._robot_type = Type("robot", ["fingers"])
        self._screw_type = Type("screw", [
            "pose_x", "pose_y", "shape", "is_fastened", "is_held"])
        self._screwdriver_type = Type("screwdriver", [
            "pose_x", "pose_y", "shape", "size", "is_held"])
        self._nail_type = Type("nail", [
            "pose_x", "pose_y", "is_fastened", "is_held"])
        self._hammer_type = Type("hammer", [
            "pose_x", "pose_y", "size", "is_held"])
        self._bolt_type = Type("bolt", [
            "pose_x", "pose_y", "is_fastened", "is_held"])
        self._wrench_type = Type("wrench", [
            "pose_x", "pose_y", "size", "is_held"])
        self._contraption_type = Type("contraption", [
            "pose_lx", "pose_ly", "pose_ux", "pose_uy"])
        # Predicates
        self._HandEmpty = Predicate(
            "HandEmpty", [self._robot_type], self._HandEmpty_holds)
        self._HoldingScrew = Predicate(
            "HoldingScrew", [self._screw_type], self._Holding_holds)
        self._HoldingScrewdriver = Predicate(
            "HoldingScrewdriver", [self._screwdriver_type], self._Holding_holds)
        self._HoldingNail = Predicate(
            "HoldingNail", [self._nail_type], self._Holding_holds)
        self._HoldingHammer = Predicate(
            "HoldingHammer", [self._hammer_type], self._Holding_holds)
        self._HoldingBolt = Predicate(
            "HoldingBolt", [self._bolt_type], self._Holding_holds)
        self._HoldingWrench = Predicate(
            "HoldingWrench", [self._wrench_type], self._Holding_holds)
        self._ScrewPlaced = Predicate(
            "ScrewPlaced", [self._screw_type, self._contraption_type],
            self._Placed_holds)
        self._NailPlaced = Predicate(
            "NailPlaced", [self._nail_type, self._contraption_type],
            self._Placed_holds)
        self._BoltPlaced = Predicate(
            "BoltPlaced", [self._bolt_type, self._contraption_type],
            self._Placed_holds)
        self._ScrewFastened = Predicate(
            "ScrewFastened", [self._screw_type], self._Fastened_holds)
        self._NailFastened = Predicate(
            "NailFastened", [self._nail_type], self._Fastened_holds)
        self._BoltFastened = Predicate(
            "BoltFastened", [self._bolt_type], self._Fastened_holds)
        self._ScrewdriverGraspable = Predicate(
            "ScrewdriverGraspable", [self._screwdriver_type],
            self._ScrewdriverGraspable_holds)
        self._HammerGraspable = Predicate(
            "HammerGraspable", [self._hammer_type],
            self._HammerGraspable_holds)
        # Options
        self._PickScrew = ParameterizedOption(
            # variables: [robot, screw to pick]
            # params: []
            "PickScrew",
            types=[self._robot_type, self._screw_type],
            params_space=Box(0, 1, (0, )),  # no parameters
            _policy=self._Pick_policy,
            _initiable=utils.always_initiable,
            _terminal=utils.onestep_terminal)
        self._PickScrewdriver = ParameterizedOption(
            # variables: [robot, screwdriver to pick]
            # params: []
            "PickScrewdriver",
            types=[self._robot_type, self._screwdriver_type],
            params_space=Box(0, 1, (0, )),  # no parameters
            _policy=self._Pick_policy,
            _initiable=utils.always_initiable,
            _terminal=utils.onestep_terminal)
        self._PickNail = ParameterizedOption(
            # variables: [robot, nail to pick]
            # params: []
            "PickNail",
            types=[self._robot_type, self._nail_type],
            params_space=Box(0, 1, (0, )),  # no parameters
            _policy=self._Pick_policy,
            _initiable=utils.always_initiable,
            _terminal=utils.onestep_terminal)
        self._PickHammer = ParameterizedOption(
            # variables: [robot, hammer to pick]
            # params: []
            "PickHammer",
            types=[self._robot_type, self._hammer_type],
            params_space=Box(0, 1, (0, )),  # no parameters
            _policy=self._Pick_policy,
            _initiable=utils.always_initiable,
            _terminal=utils.onestep_terminal)
        self._PickBolt = ParameterizedOption(
            # variables: [robot, bolt to pick]
            # params: []
            "PickBolt",
            types=[self._robot_type, self._bolt_type],
            params_space=Box(0, 1, (0, )),  # no parameters
            _policy=self._Pick_policy,
            _initiable=utils.always_initiable,
            _terminal=utils.onestep_terminal)
        self._PickWrench = ParameterizedOption(
            # variables: [robot, wrench to pick]
            # params: []
            "PickWrench",
            types=[self._robot_type, self._wrench_type],
            params_space=Box(0, 1, (0, )),  # no parameters
            _policy=self._Pick_policy,
            _initiable=utils.always_initiable,
            _terminal=utils.onestep_terminal)
        self._Place = ParameterizedOption(
            # variables: [robot]
            # params: [absolute x, absolute y]
            "Place",
            types=[self._robot_type],
            params_space=Box(
                np.array([self.table_lx, self.table_ly], dtype=np.float32),
                np.array([self.table_ux, self.table_uy], dtype=np.float32)),
            _policy=self._Place_policy,
            _initiable=utils.always_initiable,
            _terminal=utils.onestep_terminal)
        self._FastenScrewWithScrewdriver = ParameterizedOption(
            # variables: [robot, screw, screwdriver]
            # params: []
            "FastenScrewWithScrewdriver",
            types=[self._robot_type, self._screw_type, self._screwdriver_type,
                   self._contraption_type],
            params_space=Box(0, 1, (0, )),  # no parameters
            _policy=self._Fasten_policy,
            _initiable=utils.always_initiable,
            _terminal=utils.onestep_terminal)
        self._FastenScrewByHand = ParameterizedOption(
            # variables: [robot, screw]
            # params: []
            "FastenScrewByHand",
            types=[self._robot_type, self._screw_type, self._contraption_type],
            params_space=Box(0, 1, (0, )),  # no parameters
            _policy=self._Fasten_policy,
            _initiable=utils.always_initiable,
            _terminal=utils.onestep_terminal)
        self._FastenNailWithHammer = ParameterizedOption(
            # variables: [robot, nail, hammer]
            # params: []
            "FastenNailWithHammer",
            types=[self._robot_type, self._nail_type, self._hammer_type,
                   self._contraption_type],
            params_space=Box(0, 1, (0, )),  # no parameters
            _policy=self._Fasten_policy,
            _initiable=utils.always_initiable,
            _terminal=utils.onestep_terminal)
        self._FastenBoltWithWrench = ParameterizedOption(
            # variables: [robot, bolt, wrench]
            # params: []
            "FastenBoltWithWrench",
            types=[self._robot_type, self._bolt_type, self._wrench_type,
                   self._contraption_type],
            params_space=Box(0, 1, (0, )),  # no parameters
            _policy=self._Fasten_policy,
            _initiable=utils.always_initiable,
            _terminal=utils.onestep_terminal)
        # Objects
        self._robot = Object("robby", self._robot_type)

    def simulate(self, state: State, action: Action) -> State:
        assert self.action_space.contains(action.arr)
        next_state = state.copy()
        x, y, is_place = action.arr
        held = self._get_held_object(state)
        if is_place > 0.5:
            # Handle placing
            if held is None:
                # Failure: not holding anything
                return next_state
            if self._is_tool(held) and \
               self._get_contraption_pose_is_on(state, x, y) is not None:
                # Failure: cannot place a tool on a contraption
                return next_state
            next_state.set(held, "is_held", 0.0)
            next_state.set(held, "pose_x", x)
            next_state.set(held, "pose_y", y)
            next_state.set(self._robot, "fingers", 1.0)
            return next_state
        target = self._get_object_at(state, x, y)
        if target is None:
            # Failure: not placing, so something must be at this (x, y)
            return next_state
        del x, y  # no longer needed
        pose_x = state.get(target, "pose_x")
        pose_y = state.get(target, "pose_y")
        contraption = self._get_contraption_pose_is_on(state, pose_x, pose_y)
        if contraption is not None:
            assert self._is_item(target)  # tool can't be on contraption...
            # Handle fastening
            if target.type == self._screw_type:
                if held != self._get_best_screwdriver_or_none(state, target):
                    # Failure: held object doesn't match desired screwdriver
                    #          (or None if screw fastening should be by hand)
                    return next_state
            if target.type == self._nail_type:
                if held is None or held.type != self._hammer_type:
                    # Failure: need a hammer for fastening nail
                    return next_state
            if target.type == self._bolt_type:
                if held is None or held.type != self._wrench_type:
                    # Failure: need a wrench for fastening bolt
                    return next_state
            next_state.set(target, "is_fastened", 1.0)
            return next_state
        # Handle picking
        if held is not None:
            # Failure: holding something already
            return next_state
        if self._is_screwdriver_or_hammer(target) and \
           state.get(target, "size") > 0.5:
            # Failure: screwdriver/hammer is not graspable
            return next_state
        next_state.set(target, "is_held", 1.0)
        next_state.set(self._robot, "fingers", 0.0)
        return next_state

    def get_train_tasks(self) -> List[Task]:
        return self._get_tasks(
            num_tasks=CFG.num_train_tasks,
            num_items_lst=CFG.tools_num_items_train,
            num_contraptions_lst=CFG.tools_num_contraptions_train,
            rng=self._train_rng)

    def get_test_tasks(self) -> List[Task]:
        return self._get_tasks(
            num_tasks=CFG.num_test_tasks,
            num_items_lst=CFG.tools_num_items_test,
            num_contraptions_lst=CFG.tools_num_contraptions_test,
            rng=self._test_rng)

    @property
    def predicates(self) -> Set[Predicate]:
        return {self._HandEmpty, self._HoldingScrew, self._HoldingScrewdriver,
                self._HoldingNail, self._HoldingHammer, self._HoldingBolt,
                self._HoldingWrench, self._ScrewPlaced, self._NailPlaced,
                self._BoltPlaced, self._ScrewFastened, self._NailFastened,
                self._BoltFastened, self._ScrewdriverGraspable,
                self._HammerGraspable}

    @property
    def goal_predicates(self) -> Set[Predicate]:
        return {self._ScrewPlaced, self._NailPlaced, self._BoltPlaced,
                self._ScrewFastened, self._NailFastened, self._BoltFastened}

    @property
    def types(self) -> Set[Type]:
        return {self._robot_type, self._screw_type, self._screwdriver_type,
                self._nail_type, self._hammer_type, self._bolt_type,
                self._wrench_type, self._contraption_type}

    @property
    def options(self) -> Set[ParameterizedOption]:
        return {self._PickScrew, self._PickScrewdriver, self._PickNail,
                self._PickHammer, self._PickBolt, self._PickWrench,
                self._Place, self._FastenScrewWithScrewdriver,
                self._FastenNailWithHammer, self._FastenBoltWithWrench,
                self._FastenScrewByHand}

    @property
    def action_space(self) -> Box:
        # Actions are 3-dimensional vectors: [x, y, is_place bit]
        return Box(np.array([self.table_lx, self.table_ly, 0],
                            dtype=np.float32),
                   np.array([self.table_ux, self.table_uy, 1],
                            dtype=np.float32))

    def render(self,
               state: State,
               task: Task,
               action: Optional[Action] = None) -> List[Image]:
        raise NotImplementedError  # TODO

    def _get_tasks(self, num_tasks: int, num_items_lst: List[int],
                   num_contraptions_lst: List[int], rng: np.random.Generator
                   ) -> List[Task]:
        tasks = []
        for i in range(num_tasks):
            num_items = num_items_lst[i % len(num_items_lst)]
            num_contraptions = num_contraptions_lst[
                i % len(num_contraptions_lst)]
            data = {}
            # Initialize robot
            data[self._robot] = np.array([1.0])  # fingers start off open
            contraptions = []
            # Initialize contraptions
            for j in range(num_contraptions):
                contraption = Object(f"contraption{j}", self._contraption_type)
                while True:
                    pose_lx = rng.uniform(
                        self.table_lx, self.table_ux - self.contraption_size)
                    pose_ly = rng.uniform(
                        self.table_ly, self.table_uy - self.contraption_size)
                    pose_ux = pose_lx + self.contraption_size
                    pose_uy = pose_ly + self.contraption_size
                    # Make sure no other contraption intersects with this one
                    if all(data[other][2] < pose_lx or \
                           data[other][0] > pose_ux or \
                           data[other][3] < pose_ly or \
                           data[other][1] > pose_uy for other in contraptions):
                        break
                contraptions.append(contraption)
                data[contraption] = np.array(
                    [pose_lx, pose_ly, pose_ux, pose_uy], dtype=np.float32)
            # Initialize items (screws, nails, bolts) and set goal
            # We enforce that there can only be at most one screw, to make
            # the problems generally easier to solve
            items = []
            screw_cnt, nail_cnt, bolt_cnt = 0, 0, 0
            screw_created = False
            goal = set()
            for _ in range(num_items):
                while True:
                    pose_x = rng.uniform(self.table_lx, self.table_ux)
                    pose_y = rng.uniform(self.table_ly, self.table_uy)
                    # If collision with any contraption, try again
                    if any(data[c][0] < pose_x < data[c][2] and \
                           data[c][1] < pose_y < data[c][3]
                           for c in contraptions):
                        continue
                    # If collision with any item, try again
                    if any(abs(data[i][0] - pose_x) < self.close_thresh and \
                           abs(data[i][1] - pose_y) < self.close_thresh
                           for i in items):
                        continue
                    # Otherwise, we found a valid pose
                    break
                is_fastened = 0.0  # always start off not fastened
                is_held = 0.0  # always start off not held
                choices = ["screw", "nail", "bolt"]
                if screw_created:
                    choices.remove("screw")
                choice = rng.choice(choices)
                goal_contraption = rng.choice(contraptions)
                if choice == "screw":
                    item = Object(f"screw{screw_cnt}", self._screw_type)
                    screw_cnt += 1
                    shape = rng.uniform(0, 1)
                    feats = [pose_x, pose_y, shape, is_fastened, is_held]
                    goal.add(GroundAtom(self._ScrewFastened, [item]))
                    goal.add(GroundAtom(
                        self._ScrewPlaced, [item, goal_contraption]))
                    screw_created = True
                elif choice == "nail":
                    item = Object(f"nail{nail_cnt}", self._nail_type)
                    nail_cnt += 1
                    feats = [pose_x, pose_y, is_fastened, is_held]
                    goal.add(GroundAtom(self._NailFastened, [item]))
                    goal.add(GroundAtom(
                        self._NailPlaced, [item, goal_contraption]))
                elif choice == "bolt":
                    item = Object(f"bolt{bolt_cnt}", self._bolt_type)
                    bolt_cnt += 1
                    feats = [pose_x, pose_y, is_fastened, is_held]
                    goal.add(GroundAtom(self._BoltFastened, [item]))
                    goal.add(GroundAtom(
                        self._BoltPlaced, [item, goal_contraption]))
                items.append(item)
                data[item] = np.array(feats, dtype=np.float32)
            # Initialize tools (screwdrivers, hammers, wrenches)
            # We will always generate the same number of tools:
            # 3 screwdrivers (two graspable, one not)
            # 2 hammers (one graspable, one not)
            # 1 wrench (wrenches are always graspable)
            tools = []
            screwdriver_sizes = [rng.uniform(0, 0.5) for _ in range(3)]
            screwdriver_sizes[rng.integers(3)] = rng.uniform(0.5, 1)
            hammer_sizes = [rng.uniform(0, 0.5) for _ in range(2)]
            hammer_sizes[rng.integers(2)] = rng.uniform(0.5, 1)
            wrench_sizes = [rng.uniform(0, 1) for _ in range(1)]
            sizes = screwdriver_sizes + hammer_sizes + wrench_sizes
            for j, size in enumerate(sizes):
                while True:
                    pose_x = rng.uniform(self.table_lx, self.table_ux)
                    pose_y = rng.uniform(self.table_ly, self.table_uy)
                    # If collision with any contraption, try again
                    if any(data[c][0] < pose_x < data[c][2] and \
                           data[c][1] < pose_y < data[c][3]
                           for c in contraptions):
                        continue
                    # If collision with any item, try again
                    if any(abs(data[i][0] - pose_x) < self.close_thresh and \
                           abs(data[i][1] - pose_y) < self.close_thresh
                           for i in items):
                        continue
                    # If collision with any tool, try again
                    if any(abs(data[t][0] - pose_x) < self.close_thresh and \
                           abs(data[t][1] - pose_y) < self.close_thresh
                           for t in tools):
                        continue
                    # Otherwise, we found a valid pose
                    break
                is_held = 0.0  # always start off not held
                if j < 3:
                    tool = Object(f"screwdriver{j}", self._screwdriver_type)
                    shape = rng.uniform(0, 1)
                    feats = [pose_x, pose_y, shape, size, is_held]
                elif j < 5:
                    tool = Object(f"hammer{j - 3}", self._hammer_type)
                    feats = [pose_x, pose_y, size, is_held]
                else:
                    tool = Object(f"wrench{j - 5}", self._wrench_type)
                    feats = [pose_x, pose_y, size, is_held]
                tools.append(tool)
                data[tool] = np.array(feats, dtype=np.float32)
            state = State(data)
            tasks.append(Task(state, goal))
        return tasks

    @staticmethod
    def _HandEmpty_holds(state: State, objects: Sequence[Object]) -> bool:
        robot, = objects
        return state.get(robot, "fingers") > 0.5

    @staticmethod
    def _Holding_holds(state: State, objects: Sequence[Object]) -> bool:
        # Works for any item or tool
        item_or_tool, = objects
        return state.get(item_or_tool, "is_held") > 0.5

    def _Placed_holds(self, state: State, objects: Sequence[Object]) -> bool:
        # Works for any item
        item, contraption = objects
        pose_x = state.get(item, "pose_x")
        pose_y = state.get(item, "pose_y")
        return self._is_pose_on_contraption(state, pose_x, pose_y, contraption)

    @staticmethod
    def _Fastened_holds(state: State, objects: Sequence[Object]) -> bool:
        # Works for any item
        item, = objects
        return state.get(item, "is_fastened") > 0.5

    @staticmethod
    def _ScrewdriverGraspable_holds(
            state: State, objects: Sequence[Object]) -> bool:
        screwdriver, = objects
        return state.get(screwdriver, "size") < 0.5

    @staticmethod
    def _HammerGraspable_holds(state: State, objects: Sequence[Object]) -> bool:
        hammer, = objects
        return state.get(hammer, "size") < 0.5

    @staticmethod
    def _Pick_policy(state: State, memory: Dict, objects: Sequence[Object],
                     params: Array) -> Action:
        del memory  # unused
        assert not params
        _, item_or_tool = objects
        pose_x = state.get(item_or_tool, "pose_x")
        pose_y = state.get(item_or_tool, "pose_y")
        arr = np.array([pose_x, pose_y, 0.0], dtype=np.float32)
        return Action(arr)

    @staticmethod
    def _Place_policy(state: State, memory: Dict, objects: Sequence[Object],
                      params: Array) -> Action:
        del state, memory, objects  # unused
        return Action(np.r_[params, 1.0])

    def _Fasten_policy(self, state: State, memory: Dict,
                       objects: Sequence[Object], params: Array) -> Action:
        del memory  # unused
        assert not params
        item = objects[1]
        assert self._is_item(item)
        pose_x = state.get(item, "pose_x")
        pose_y = state.get(item, "pose_y")
        arr = np.array([pose_x, pose_y, 0.0], dtype=np.float32)
        return Action(arr)

    def _get_object_at(self, state: State, x: float, y: float
                       ) -> Optional[Object]:
        for obj in state:
            if obj == self._robot:
                continue
            if obj.type == self._contraption_type:
                continue
            if abs(state.get(obj, "pose_x") - x) < self.close_thresh and \
               abs(state.get(obj, "pose_y") - y) < self.close_thresh:
                return obj
        return None

    def _get_held_object(self, state: State) -> Optional[Object]:
        for obj in state:
            if obj == self._robot:
                continue
            if obj.type == self._contraption_type:
                continue
            if state.get(obj, "is_held") > 0.5:
                return obj
        return None

    def _get_contraption_pose_is_on(self, state: State, x: float, y: float
                                    ) -> Optional[Object]:
        for obj in state:
            if obj.type != self._contraption_type:
                continue
            if self._is_pose_on_contraption(state, x, y, obj):
                return obj
        return None

    @staticmethod
    def _is_pose_on_contraption(state: State, x: float, y: float,
                                contraption: Object) -> bool:
        pose_lx = state.get(contraption, "pose_lx")
        pose_ly = state.get(contraption, "pose_ly")
        pose_ux = state.get(contraption, "pose_ux")
        pose_uy = state.get(contraption, "pose_uy")
        return pose_lx < x < pose_ux and pose_ly < y < pose_uy

    def _is_tool(self, obj: Object) -> bool:
        return obj.type in (
            self._screwdriver_type, self._hammer_type, self._wrench_type)

    def _is_item(self, obj: Object) -> bool:
        return obj.type in (self._screw_type, self._nail_type, self._bolt_type)

    def _is_screwdriver_or_hammer(self, obj: Object) -> bool:
        return obj.type in (self._screwdriver_type, self._hammer_type)

    def _get_best_screwdriver_or_none(self, state: State, screw: Object
                                      ) -> Optional[Object]:
        """Use the shape of the given screw to figure out the best graspable
        screwdriver for it, or None if no graspable screwdriver has a shape
        within the threshold self.screw_shape_hand_thresh.
        """
        assert screw.type == self._screw_type
        closest_screwdriver = None
        closest_diff = float("inf")
        screw_shape = state.get(screw, "shape")
        for obj in state:
            if obj.type != self._screwdriver_type:
                continue
            if state.get(obj, "size") > 0.5:
                # Ignore non-graspable screwdrivers
                continue
            screwdriver_shape = state.get(obj, "shape")
            diff = abs(screw_shape - screwdriver_shape)
            if diff < closest_diff:
                closest_diff = diff
                closest_screwdriver = obj
        if closest_diff > self.screw_shape_hand_thresh:
            return None
        return closest_screwdriver
