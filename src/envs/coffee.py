"""An environment where a robot must brew and pour coffee."""

from typing import ClassVar, Dict, List, Optional, Sequence, Set, Tuple

import matplotlib.pyplot as plt
import numpy as np
from gym.spaces import Box

from predicators.src import utils
from predicators.src.envs import BaseEnv
from predicators.src.settings import CFG
from predicators.src.structs import Action, Array, GroundAtom, Image, Object, \
    ParameterizedOption, Predicate, State, Task, Type


class CoffeeEnv(BaseEnv):
    """An environment where a robot must brew and pour coffee."""

    x_lb: ClassVar[float] = 0.0
    x_ub: ClassVar[float] = 10.0
    y_lb: ClassVar[float] = 0.0
    y_ub: ClassVar[float] = 10.0
    z_lb: ClassVar[float] = 0.0
    z_ub: ClassVar[float] = 10.0
    tilt_lb: ClassVar[float] = 0.0
    tilt_ub: ClassVar[float] = np.pi / 2
    init_padding: ClassVar[float] = 0.5  # used to space objects in init states
    robot_init_x: ClassVar[float] = (x_ub + x_lb) / 2.0
    robot_init_y: ClassVar[float] = (y_ub + y_lb) / 2.0
    robot_init_z: ClassVar[float] = z_ub
    robot_init_tilt: ClassVar[float] = 0.0
    open_fingers: ClassVar[float] = 0.4
    closed_fingers: ClassVar[float] = 0.1
    machine_x_len: ClassVar[float] = 0.1 * (x_ub - x_lb)
    machine_y_len: ClassVar[float] = 0.2 * (y_ub - y_lb)
    machine_x: ClassVar[float] = x_ub - machine_x_len - init_padding
    machine_y: ClassVar[float] = y_ub - machine_y_len - init_padding
    jug_radius: ClassVar[float] = (0.8 * machine_x_len) / 2.0
    jug_height: ClassVar[float] = 0.2 * (z_ub - z_lb)
    cup_radius: ClassVar[float] = 0.3 * jug_radius
    jug_init_x_lb: ClassVar[float] = machine_x - machine_x_len + init_padding
    jug_init_x_ub: ClassVar[float] = machine_x + machine_x_len - init_padding
    jug_init_y_lb: ClassVar[float] = y_lb + jug_radius + init_padding
    jug_init_y_ub: ClassVar[
        float] = machine_y - machine_y_len - jug_radius - init_padding
    jug_handle_x_offset: ClassVar[float] = 0.0
    jug_handle_y_offset: ClassVar[float] = -(1.05 * jug_radius)
    jug_handle_height: ClassVar[float] = 3 * jug_height / 4
    cup_init_x_lb: ClassVar[float] = x_lb + cup_radius + init_padding
    cup_init_x_ub: ClassVar[
        float] = machine_x - machine_x_len - cup_radius - init_padding
    cup_init_y_lb: ClassVar[float] = jug_init_y_lb
    cup_init_y_ub: ClassVar[float] = jug_init_y_ub
    cup_capacity_lb: ClassVar[float] = 0.075 * (z_ub - z_lb)
    cup_capacity_ub: ClassVar[float] = 0.15 * (z_ub - z_lb)
    cup_target_frac: ClassVar[float] = 0.75  # fraction of the capacity
    max_position_vel: ClassVar[float] = 0.5
    max_angular_vel: ClassVar[float] = np.pi / 10
    max_finger_vel: ClassVar[float] = 1.0
    grasp_finger_tol: ClassVar[float] = 1e-2
    grasp_position_tol: ClassVar[float] = 1e-1

    def __init__(self) -> None:
        super().__init__()

        # Types
        self._robot_type = Type("robot", ["x", "y", "z", "tilt", "fingers"])
        self._jug_type = Type("jug", ["x", "y", "is_held", "is_filled"])
        self._machine_type = Type("machine", ["is_on"])
        self._cup_type = Type(
            "cup",
            ["x", "y", "capacity_liquid", "target_liquid", "current_liquid"])

        # Predicates
        self._CupFilled = Predicate("CupFilled", [self._cup_type],
                                    self._CupFilled_holds)
        # TODO

        # Options
        # TODO

        # Static objects (always exist no matter the settings).
        self._robot = Object("robby", self._robot_type)
        self._jug = Object("juggy", self._jug_type)
        self._machine = Object("coffee_machine", self._machine_type)

    @classmethod
    def get_name(cls) -> str:
        return "coffee"

    def simulate(self, state: State, action: Action) -> State:
        assert self.action_space.contains(action.arr)
        next_state = state.copy()
        norm_dx, norm_dy, norm_dz, norm_dtilt, norm_dfingers = action.arr
        # Denormalize the action.
        dx = norm_dx * self.max_position_vel
        dy = norm_dy * self.max_position_vel
        dz = norm_dz * self.max_position_vel
        dtilt = norm_dtilt * self.max_angular_vel
        dfingers = norm_dfingers * self.max_finger_vel
        # Apply changes to the robot, taking bounds into account.
        x = np.clip(state.get(self._robot, "x") + dx, self.x_lb, self.x_ub)
        y = np.clip(state.get(self._robot, "y") + dy, self.y_lb, self.y_ub)
        z = np.clip(state.get(self._robot, "z") + dz, self.z_lb, self.z_ub)
        tilt = np.clip(
            state.get(self._robot, "tilt") + dtilt, self.tilt_lb, self.tilt_ub)
        fingers = np.clip(
            state.get(self._robot, "fingers") + dfingers, self.closed_fingers,
            self.open_fingers)
        # Make sure we don't use the deltas because they may now be wrong
        # after clipping.
        del dx, dy, dz, dtilt, dfingers
        # Update the robot in the new state.
        next_state.set(self._robot, "x", x)
        next_state.set(self._robot, "y", y)
        next_state.set(self._robot, "z", z)
        next_state.set(self._robot, "tilt", tilt)
        next_state.set(self._robot, "fingers", fingers)
        # Check if the jug should be grasped for the first time.
        if state.get(self._jug, "is_held") < 0.5 and \
            abs(fingers < self.closed_fingers) < self.grasp_finger_tol:
            handle_pos = self._get_jug_handle_grasp(state, self._jug)
            sq_dist_to_handle = np.sum(np.subtract(handle_pos, (x, y, z))**2)
            if sq_dist_to_handle < self.grasp_position_tol:
                # Grasp the jug.
                next_state.set(self._jug, "is_held", 1.0)

        return next_state

    def _generate_train_tasks(self) -> List[Task]:
        return self._get_tasks(num=CFG.num_train_tasks,
                               num_cups_lst=CFG.coffee_num_cups_train,
                               rng=self._train_rng)

    def _generate_test_tasks(self) -> List[Task]:
        return self._get_tasks(num=CFG.num_test_tasks,
                               num_cups_lst=CFG.coffee_num_cups_test,
                               rng=self._test_rng)

    @property
    def predicates(self) -> Set[Predicate]:
        return {self._CupFilled}  # TODO

    @property
    def goal_predicates(self) -> Set[Predicate]:
        return {self._CupFilled}

    @property
    def types(self) -> Set[Type]:
        return {
            self._cup_type, self._jug_type, self._machine_type,
            self._robot_type
        }

    @property
    def options(self) -> Set[ParameterizedOption]:
        return set()  # TODO

    @property
    def action_space(self) -> Box:
        # Normalized dx, dy, dz, dtilt, dfingers.
        return Box(low=-1., high=1., shape=(5, ), dtype=np.float32)

    def render_state(self,
                     state: State,
                     task: Task,
                     action: Optional[Action] = None,
                     caption: Optional[str] = None) -> List[Image]:
        del caption  # unused
        # A crude top-down rendering.
        figsize = (self.x_ub - self.x_lb, self.y_ub - self.y_lb)
        fig, ax = plt.subplots(1, 1, figsize=figsize)
        # Draw the cups.
        for cup in state.get_objects(self._cup_type):
            # TODO make color indicate filled level
            color = "salmon"
            x = state.get(cup, "x")
            y = state.get(cup, "y")
            circ = utils.Circle(x, y, self.cup_radius)
            circ.plot(ax, facecolor=color, edgecolor="black")
        # Draw the machine.
        machine, = state.get_objects(self._machine_type)
        color = "gray"  # TODO change color if the machine is on
        rect = utils.Rectangle(x=self.machine_x,
                               y=self.machine_y,
                               width=self.machine_x_len,
                               height=self.machine_y_len,
                               theta=0.0)
        rect.plot(ax, facecolor=color, edgecolor="black")
        # Draw the jug.
        jug, = state.get_objects(self._jug_type)
        if state.get(jug, "is_held") < 0.5:
            color = "lightgreen"
        else:
            color = "darkgreen"  # TODO change if full of liquid
        x = state.get(jug, "x")
        y = state.get(jug, "y")
        circ = utils.Circle(x=x, y=y, radius=self.jug_radius)
        circ.plot(ax, facecolor=color, edgecolor="black")
        # Draw the robot.
        color = "gold"
        robot, = state.get_objects(self._robot_type)
        x = state.get(robot, "x")
        y = state.get(robot, "y")
        circ = utils.Circle(
            x=x,
            y=y,
            radius=self.cup_radius  # robot in reality has no 'radius'
        )
        circ.plot(ax, facecolor=color, edgecolor="black")
        ax.set_xlim(self.x_lb, self.x_ub)
        ax.set_ylim(self.y_lb, self.y_ub)
        ax.axis("off")
        plt.tight_layout()
        img = utils.fig2data(fig)
        plt.close()
        return [img]

    def _get_tasks(self, num: int, num_cups_lst: List[int],
                   rng: np.random.Generator) -> List[Task]:
        tasks = []
        # Create the parts of the initial state that do not change between
        # tasks, which includes the robot and the machine.
        common_state_dict = {}
        # Create the robot.
        common_state_dict[self._robot] = {
            "x": self.robot_init_x,
            "y": self.robot_init_y,
            "z": self.robot_init_z,
            "tilt": self.robot_init_tilt,
            "fingers": self.open_fingers,  # robot fingers start open
        }
        # Create the machine.
        common_state_dict[self._machine] = {
            "is_on": 0.0,  # machine starts off
        }
        for _ in range(num):
            state_dict = {k: v.copy() for k, v in common_state_dict.items()}
            num_cups = num_cups_lst[rng.choice(len(num_cups_lst))]
            cups = [Object(f"cup{i}", self._cup_type) for i in range(num_cups)]
            goal = {GroundAtom(self._CupFilled, [c]) for c in cups}
            # Sample initial positions for cups, making sure to keep them
            # far enough apart from one another.
            collision_geoms: Set[utils.Circle] = set()
            radius = self.cup_radius + self.init_padding
            for cup in cups:
                # Assuming that the dimensions are forgiving enough that
                # infinite loops are impossible.
                while True:
                    x = rng.uniform(self.cup_init_x_lb, self.cup_init_x_ub)
                    y = rng.uniform(self.cup_init_y_lb, self.cup_init_y_ub)
                    geom = utils.Circle(x, y, radius)
                    # Keep only if no intersections with existing objects.
                    if not any(geom.intersects(g) for g in collision_geoms):
                        break
                collision_geoms.add(geom)
                # Sample a cup capacity, which also defines the cup's height.
                cap = rng.uniform(self.cup_capacity_lb, self.cup_capacity_ub)
                # Target liquid amount for filling the cup.
                target = cap * self.cup_target_frac
                # The initial liquid amount is always 0.
                current = 0.0
                state_dict[cup] = {
                    "x": x,
                    "y": y,
                    "capacity_liquid": cap,
                    "target_liquid": target,
                    "current_liquid": current,
                }
            # Create the jug.
            x = rng.uniform(self.jug_init_x_lb, self.jug_init_x_ub)
            y = rng.uniform(self.jug_init_y_lb, self.jug_init_y_ub)
            state_dict[self._jug] = {
                "x": x,
                "y": y,
                "is_held": 0.0,  # jug starts off not held
                "is_filled": 0.0  # jug starts off empty
            }
            init_state = utils.create_state_from_dict(state_dict)
            task = Task(init_state, goal)
            tasks.append(task)
        return tasks

    @staticmethod
    def _CupFilled_holds(state: State, objects: Sequence[Object]) -> bool:
        cup, = objects
        current = state.get(cup, "current_liquid")
        target = state.get(cup, "target_liquid")
        return current > target

    def _get_jug_handle_grasp(self, state: State,
                              jug: Object) -> Tuple[float, float, float]:
        target_x = state.get(jug, "x") + self.jug_handle_x_offset
        target_y = state.get(jug, "y") + self.jug_handle_y_offset
        target_z = self.jug_handle_height
        return (target_x, target_y, target_z)
