from typing import Any, ClassVar, Dict, List, Optional, Sequence, Set, Tuple, \
    Union

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from gym.spaces import Box
from matplotlib import patches

from predicators import utils
from predicators.envs import BaseEnv
from predicators.settings import CFG
from predicators.structs import Action, Array, GroundAtom, Object, \
    ParameterizedOption, Predicate, State, Task, Type


class StickbasketEnv(BaseEnv):
    """Stickbasket domain."""

    env_x_lb: ClassVar[float] = 0
    env_y_lb: ClassVar[float] = 0
    env_x_ub: ClassVar[float] = 20#12#
    env_y_ub: ClassVar[float] = 20#12#
    robot_radius: ClassVar[float] = 2
    gripper_length: ClassVar[float] = 2
    basket_w_lb: ClassVar[float] = 7
    basket_w_ub: ClassVar[float] = 10#7#
    basket_h_lb: ClassVar[float] = 3
    basket_h_ub: ClassVar[float] = 5
    stick_w_lb: ClassVar[float] = 0.5
    stick_w_ub: ClassVar[float] = 1
    stick_h_lb: ClassVar[float] = 5
    stick_h_ub: ClassVar[float] = 6
    _basket_color: ClassVar[List[float]] = [0.89, 0.82, 0.68]
    _robot_color: ClassVar[List[float]] = [0.5, 0.5, 0.5]

    def __init__(self, use_gui: bool = True) -> None:
        super().__init__(use_gui)

        # Types
        self._object_type = Type("object", ["pose_x", "pose_y", "width", "height", "yaw", "held"])
        self._pickable_type = Type("pickable", ["pose_x", "pose_y", "width", "height", "yaw", "held"], self._object_type)
        self._placeable_type = Type("placeable", ["pose_x", "pose_y", "width", "height", "yaw", "held"], self._object_type)
        # Stick: when held, pose becomes relative to gripper
        self._stick_type = Type(
            "stick", ["pose_x", "pose_y", "width", "height", "yaw", "held"],
            self._pickable_type)
        self._basket_type = Type("basket",
                                ["pose_x", "pose_y", "width", "height", "yaw", "held"],
                                self._placeable_type)
        self._robot_type = Type("robot",
                                ["pose_x", "pose_y", "yaw", "gripper_free"])
        # Predicates
        self._OnBasket = Predicate("OnBasket",
                                  [self._stick_type, self._basket_type],
                                  self._OnBasket_holds)
        self._CanReach = Predicate("CanReach",
                                  [self._object_type, self._robot_type],
                                  self._CanReach_holds)
        self._Holding = Predicate("Holding", [self._stick_type],
                                  self._Holding_holds)
        self._GripperFree = Predicate("GripperFree", [self._robot_type],
                                      self._GripperFree_holds)
        # Options
        if CFG.bookshelf_add_sampler_idx_to_params:
            if CFG.use_fully_uniform_actions:
                lo = [0, self.env_x_lb, self.env_y_lb]
                hi = [2, self.env_x_ub, self.env_y_ub]
            else:
                lo = [0, -8, -4]
                hi = [2, 9, 5]
        else:
            if CFG.use_fully_uniform_actions:
                lo = [self.env_x_lb, self.env_y_lb]
                hi = [self.env_x_ub, self.env_y_ub]
            else:
                lo = [-8, -4]
                hi = [9, 5]
        self._NavigateTo = utils.SingletonParameterizedOption(
            # variables: [robot, object to navigate to]
            # params: [offset_x, offset_y]
            "NavigateTo",
            self._NavigateTo_policy,
            types=[self._robot_type, self._object_type],
            params_space=Box(np.array(lo, dtype=np.float32),
                             np.array(hi, dtype=np.float32)))
        if CFG.bookshelf_add_sampler_idx_to_params:
            lo = [0, 0, -np.pi]
            hi = [2, 1, np.pi]
        else:
            lo = [0, -np.pi]
            hi = [1, np.pi]
        self._PickStick = utils.SingletonParameterizedOption(
            # variables: [robot, stick to pick]
            # params: [offset_gripper, stick_yaw]
            "PickStick",
            self._PickStick_policy,
            types=[self._robot_type, self._stick_type],
            params_space=Box(np.array(lo, dtype=np.float32),
                             np.array(hi, dtype=np.float32)))
        if CFG.bookshelf_add_sampler_idx_to_params:
            lo = [0, 0]
            hi = [2, 1]
        else:
            lo = [0]
            hi = [1]
        self._PlaceStickOnBasket = utils.SingletonParameterizedOption(
            # variables: [robot, stick, basket]
            # params: [offset_gripper]
            "PlaceStickOnBasket",
            self._PlaceStickOnBasket_policy,
            types=[self._robot_type, self._stick_type, self._basket_type],
            params_space=Box(np.array(lo, dtype=np.float32),
                             np.array(hi, dtype=np.float32)))
        # Static objects (always exist no matter the settings)
        self._basket = Object("basket", self._basket_type)
        self._robot = Object("robby", self._robot_type)

    @classmethod
    def get_name(cls) -> str:
        return "stickbasket"

    def simulate(self, state: State, action: Action) -> State:
        assert self.action_space.contains(action.arr)
        arr = action.arr
        if arr[-1] < -0.33:
            transition_fn = self._transition_pick_stick
        elif -0.33 <= arr[-1] < 0.33:
            transition_fn = self._transition_place_stick_on_basket
        elif 0.33 <= arr[-1]:
            transition_fn = self._transition_navigate_to
        return transition_fn(state, action)

    def _transition_pick_stick(self, state: State, action: Action) -> State:
        yaw, offset_gripper = action.arr[2:4]
        next_state = state.copy()
        held_stick = self._get_held_stick(state)
        if held_stick is not None:
            return next_state

        robby = self._robot
        robby_x = state.get(robby, "pose_x")
        robby_y = state.get(robby, "pose_y")
        robby_yaw = state.get(robby, "yaw")
        gripper_free = state.get(robby, "gripper_free")
        if gripper_free != 1.0:
            return next_state

        tip_x = robby_x + (self.robot_radius + offset_gripper *
                           self.gripper_length) * np.cos(robby_yaw)
        tip_y = robby_y + (self.robot_radius + offset_gripper *
                           self.gripper_length) * np.sin(robby_yaw)

        pick_stick, stick_rect = self._get_pickable_stick(state, tip_x, tip_y)
        gripper_line = utils.LineSegment(robby_x, robby_y, tip_x, tip_y)
        ignore_objects = {pick_stick, robby}
        # Check that there is a graspable stick and that there are no gripper collisions
        if pick_stick is None or self.check_collision(state, gripper_line,
                                                     ignore_objects):
            return next_state
        # Execute pick
        stick_rect = stick_rect.rotate_about_point(tip_x, tip_y, yaw)
        yaw = stick_rect.theta - robby_yaw
        while yaw > np.pi:
            yaw -= (2 * np.pi)
        while yaw < -np.pi:
            yaw += (2 * np.pi)
        rel_x = stick_rect.x - tip_x
        rel_y = stick_rect.y - tip_y
        rot_rel_x = rel_x * np.sin(robby_yaw) - rel_y * np.cos(robby_yaw)
        rot_rel_y = rel_x * np.cos(robby_yaw) + rel_y * np.sin(robby_yaw)
        next_state.set(pick_stick, "held", 1.0)
        next_state.set(pick_stick, "pose_x", rot_rel_x)
        next_state.set(pick_stick, "pose_y", rot_rel_y)
        next_state.set(pick_stick, "yaw", yaw)
        next_state.set(robby, "gripper_free", 0.0)
        return next_state

    def _transition_place_stick_on_basket(self, state: State,
                                        action: Action) -> State:
        offset_gripper = action.arr[3]
        next_state = state.copy()

        stick = self._get_held_stick(state)
        if stick is None:
            return next_state
        stick_relative_x = state.get(stick, "pose_x")
        stick_relative_y = state.get(stick, "pose_y")
        stick_relative_yaw = state.get(stick, "yaw")
        stick_w = state.get(stick, "width")
        stick_h = state.get(stick, "height")

        robby = self._robot
        robby_x = state.get(robby, "pose_x")
        robby_y = state.get(robby, "pose_y")
        robby_yaw = state.get(robby, "yaw")
        gripper_free = state.get(robby, "gripper_free")
        if gripper_free != 0.0:
            return next_state

        basket = self._basket
        basket_x = state.get(basket, "pose_x")
        basket_y = state.get(basket, "pose_y")
        basket_w = state.get(basket, "width")
        basket_h = state.get(basket, "height")
        basket_yaw = state.get(basket, "yaw")

        tip_x = robby_x + (self.robot_radius + offset_gripper *
                           self.gripper_length) * np.cos(robby_yaw)
        tip_y = robby_y + (self.robot_radius + offset_gripper *
                           self.gripper_length) * np.sin(robby_yaw)

        place_x = tip_x + stick_relative_x * np.sin(
            robby_yaw) + stick_relative_y * np.cos(robby_yaw)
        place_y = tip_y + stick_relative_y * np.sin(
            robby_yaw) - stick_relative_x * np.cos(robby_yaw)
        place_yaw = stick_relative_yaw + robby_yaw
        while place_yaw > np.pi:
            place_yaw -= (2 * np.pi)
        while place_yaw < -np.pi:
            place_yaw += (2 * np.pi)

        # Check whether the sticks center-of-mass is within the basket bounds
        # and that there are no other collisions with the stick or gripper
        stick_rect = utils.Rectangle(place_x, place_y, stick_w, stick_h,
                                    place_yaw)
        basket_rect = utils.Rectangle(basket_x, basket_y, basket_w, basket_h,
                                     basket_yaw)
        gripper_line = utils.LineSegment(robby_x, robby_y, tip_x, tip_y)
        ignore_objects = {stick, robby, basket}
        # if not basket_rect.contains_point(*(stick_rect.vertices[0])) or \
        #     not basket_rect.contains_point(*(stick_rect.vertices[1])) or \
        #     not basket_rect.contains_point(*(stick_rect.vertices[2])) or \
        #     not basket_rect.contains_point(*(stick_rect.vertices[3])) or \
        if not basket_rect.contains_point(*(stick_rect.center)) or \
            self.check_collision(state, stick_rect, ignore_objects) or \
            self.check_collision(state, gripper_line, ignore_objects):
            return next_state

        next_state.set(stick, "held", 0.0)
        next_state.set(stick, "pose_x", place_x)
        next_state.set(stick, "pose_y", place_y)
        next_state.set(stick, "yaw", place_yaw)
        next_state.set(robby, "gripper_free", 1.0)
        return next_state

    def _transition_navigate_to(self, state: State, action: Action) -> State:
        pos_x, pos_y, yaw = action.arr[:3]
        next_state = state.copy()

        robby = self._robot
        robby_geom = utils.Circle(pos_x, pos_y, self.robot_radius)
        ignore_objects = {robby}
        if self.check_collision(state, robby_geom, ignore_objects):
            return next_state

        next_state.set(robby, "pose_x", pos_x)
        next_state.set(robby, "pose_y", pos_y)
        next_state.set(robby, "yaw", yaw)
        return next_state

    @property
    def _num_sticks_train(self) -> List[int]:
        return CFG.stickbasket_num_sticks_train

    @property
    def _num_sticks_test(self) -> List[int]:
        return CFG.stickbasket_num_sticks_test

    @property
    def _num_obstacles_train(self) -> List[int]:
        return CFG.bookshelf_num_obstacles_train if not CFG.bookshelf_no_obstacles else [0]

    @property
    def _num_obstacles_test(self) -> List[int]:
        return CFG.bookshelf_num_obstacles_test if not CFG.bookshelf_no_obstacles else [0]

    def _generate_train_tasks(self) -> List[Task]:
        return self._get_tasks(num_tasks=CFG.bookshelf_train_tasks_overwrite if CFG.bookshelf_train_tasks_overwrite is not None else CFG.num_train_tasks,
                               possible_num_sticks=self._num_sticks_train,
                               possible_num_obstacles=self._num_obstacles_train,
                               rng=self._train_rng)

    def _generate_test_tasks(self) -> List[Task]:
        return self._get_tasks(num_tasks=CFG.num_test_tasks,
                               possible_num_sticks=self._num_sticks_test,
                               possible_num_obstacles=self._num_obstacles_test,
                               rng=self._test_rng)

    @property
    def predicates(self) -> Set[Predicate]:
        return {self._OnBasket, self._CanReach, self._Holding, self._GripperFree}

    @property
    def goal_predicates(self) -> Set[Predicate]:
        return {self._OnBasket}

    @property
    def types(self) -> Set[Type]:
        return {
            self._object_type, self._pickable_type, self._placeable_type,
            self._stick_type, self._basket_type, self._robot_type
        }

    @property
    def options(self) -> Set[ParameterizedOption]:
        return {self._NavigateTo, self._PickStick, self._PlaceStickOnBasket}

    @property
    def action_space(self) -> Box:
        # Actions are 5-dimensional vectors:
        # [x, y, yaw, offset_gripper, pick_place_navigate]
        # yaw is only used for pick actions
        # pick_place_navigate is -1 for pick, 0 for place, 1 for navigate
        lowers = np.array([self.env_x_lb, self.env_y_lb, -np.pi, 0.0, -1.0],
                          dtype=np.float32)
        uppers = np.array([self.env_x_ub, self.env_y_ub, np.pi, 1.0, 1.0],
                          dtype=np.float32)
        return Box(lowers, uppers)

    def render_state_plt(
            self,
            state: State,
            task: Task,
            action: Optional[Action] = None,
            caption: Optional[str] = None) -> matplotlib.figure.Figure:
        fig, ax = plt.subplots(1, 1, figsize=(6, 6))
        sticks = [b for b in state if b.is_instance(self._stick_type)]
        if task is not None:
            goal_sticks = {obj for atom in task.goal for obj in atom.objects if obj.is_instance(self._stick_type) }
        else:
            goal_sticks = sticks
        
        basket = self._basket
        robby = self._robot
        # Draw basket
        basket_x = state.get(basket, "pose_x")
        basket_y = state.get(basket, "pose_y")
        basket_w = state.get(basket, "width")
        basket_h = state.get(basket, "height")
        basket_yaw = state.get(basket, "yaw")
        rect = utils.Rectangle(basket_x, basket_y, basket_w, basket_h, basket_yaw)
        rect.plot(ax, facecolor=self._basket_color)

        # Draw robot
        robby_x = state.get(robby, "pose_x")
        robby_y = state.get(robby, "pose_y")
        robby_yaw = state.get(robby, "yaw")
        gripper_free = state.get(robby, "gripper_free") == 1.0
        circ = utils.Circle(robby_x, robby_y, self.robot_radius)
        circ.plot(ax, facecolor=self._robot_color)
        offset_gripper = 0  # if gripper_free else 1
        tip_x = robby_x + (self.robot_radius + offset_gripper *
                           self.gripper_length) * np.cos(robby_yaw)
        tip_y = robby_y + (self.robot_radius + offset_gripper *
                           self.gripper_length) * np.sin(robby_yaw)
        gripper_line = utils.LineSegment(robby_x, robby_y, tip_x, tip_y)
        gripper_line.plot(ax, color="white")

        # Draw sticks
        for b in sorted(sticks):
            x = state.get(b, "pose_x")
            y = state.get(b, "pose_y")
            w = state.get(b, "width")
            h = state.get(b, "height")
            yaw = state.get(b, "yaw")
            holding = state.get(b, "held") == 1.0
            fc = "sienna" if b in goal_sticks else "gray"
            ec = "red" if holding else "gray"
            if holding:
                aux_x = tip_x + x * np.sin(robby_yaw) + y * np.cos(robby_yaw)
                aux_y = tip_y + y * np.sin(robby_yaw) - x * np.cos(robby_yaw)
                x = aux_x
                y = aux_y
                yaw += robby_yaw
            while yaw > np.pi:
                yaw -= (2 * np.pi)
            while yaw < -np.pi:
                yaw += (2 * np.pi)
            rect = utils.Rectangle(x, y, w, h, yaw)
            rect.plot(ax, facecolor=fc, edgecolor=ec)

        ax.set_xlim(self.env_x_lb, self.env_x_ub)
        ax.set_ylim(self.env_y_lb, self.env_y_ub)
        plt.suptitle(caption, fontsize=12, wrap=True)
        plt.tight_layout()
        plt.axis("off")
        return fig

    def _get_tasks(self, num_tasks: int, possible_num_sticks: List[int],
                   possible_num_obstacles: List[int],
                   rng: np.random.Generator) -> List[Task]:
        tasks = []
        for i in range(num_tasks):
            num_sticks = rng.choice(possible_num_sticks)
            num_obstacles = rng.choice(possible_num_obstacles)
            data = {}

            # Sample basket variables
            if CFG.stickbasket_against_wall:
                raise ValueError('This env is not supposed to have the basket fixed against a wall')
                # # Size
                # basket_w = rng.uniform(self.basket_w_lb, self.basket_w_ub)
                # basket_h = rng.uniform(self.basket_h_lb, self.basket_h_ub)
                # # Pick wall
                # wall_idx = rng.integers(4)  # l, b, r, t
                # # Pose
                # if wall_idx == 0 or wall_idx == 2:
                #     basket_y = rng.uniform(self.env_y_lb + basket_w, self.env_y_ub)
                #     if wall_idx == 0:
                #         basket_x = self.env_x_lb
                #     else:
                #         basket_x = self.env_x_ub - basket_h
                #     basket_yaw = - np.pi / 2
                # else:
                #     basket_x = rng.uniform(self.env_x_lb, self.env_x_ub - basket_w)
                #     if wall_idx == 1:
                #         basket_y = self.env_y_lb
                #     else:
                #         basket_y = self.env_y_ub - basket_h
                #     basket_yaw = 0
            else:
                basket_out_of_bounds = True
                env_w = self.env_x_ub - self.env_x_lb
                env_h = self.env_y_ub - self.env_y_lb
                env_x = self.env_x_lb
                env_y = self.env_y_lb
                env_rect = utils.Rectangle(env_x, env_y, env_w, env_h, 0)
                while basket_out_of_bounds:
                    # Size
                    basket_w = rng.uniform(self.basket_w_lb, self.basket_w_ub)
                    basket_h = rng.uniform(self.basket_h_lb, self.basket_h_ub)
                    # Pose
                    basket_x = rng.uniform(self.env_x_lb, self.env_x_ub - basket_w)
                    basket_y = rng.uniform(self.env_y_lb, self.env_y_ub - basket_h)
                    basket_yaw = rng.uniform(-np.pi, np.pi)

                    basket_rect = utils.Rectangle(basket_x, basket_y, basket_w, basket_h,
                                                 basket_yaw)
                    basket_out_of_bounds = not(
                        env_rect.contains_point(*(basket_rect.vertices[0])) and
                        env_rect.contains_point(*(basket_rect.vertices[1])) and
                        env_rect.contains_point(*(basket_rect.vertices[2])) and
                        env_rect.contains_point(*(basket_rect.vertices[3]))
                    )

            data[self._basket] = np.array(
                [basket_x, basket_y, basket_w, basket_h, basket_yaw, 0.0])

            tmp_state = State(data)
            # Initialize robot pos
            robot_collision = True
            while robot_collision:
                robot_init_x = rng.uniform(self.env_x_lb, self.env_x_ub)
                robot_init_y = rng.uniform(self.env_y_lb, self.env_y_ub)
                robot_init_yaw = rng.uniform(-np.pi, np.pi)
                robot_circ = utils.Circle(robot_init_x, robot_init_y,
                                          self.robot_radius)
                robot_collision = self.check_collision(tmp_state, robot_circ)

            gripper_free = 1.0
            data[self._robot] = np.array(
                [robot_init_x, robot_init_y, robot_init_yaw, gripper_free])

            # Sample stick poses
            goal = set()
            for j in range(num_sticks):
                stick_collision = True
                tmp_state = State(data)
                while stick_collision:
                    stick_init_x = rng.uniform(self.env_x_lb, self.env_x_ub)
                    stick_init_y = rng.uniform(self.env_y_lb, self.env_y_ub)
                    stick_init_yaw = rng.uniform(-np.pi, np.pi)
                    stick_width = rng.uniform(self.stick_w_lb, self.stick_w_ub)
                    stick_height = rng.uniform(self.stick_h_lb, self.stick_h_ub)
                    stick_rect = utils.Rectangle(stick_init_x, stick_init_y,
                                                stick_width, stick_height,
                                                stick_init_yaw)
                    stick_collision = self.check_collision(tmp_state, stick_rect)
                stick = Object(f"stick{j}", self._stick_type)
                held = 0.0
                data[stick] = np.array([
                    stick_init_x, stick_init_y, stick_width, stick_height,
                    stick_init_yaw, held
                ],
                                      dtype=np.float32)
                if not CFG.stickbasket_singlestep_goal:
                    goal.add(GroundAtom(self._OnBasket, [stick, self._basket]))
                elif j == 0:
                    goal.add(GroundAtom(self._CanReach, [stick, self._robot]))

            for j in range(num_sticks, num_sticks + num_obstacles):
                obstacle_collision = True
                tmp_state = State(data)
                ignore_objects = {self._basket}   # allow obstacles to be placed on basket
                while obstacle_collision:
                    obstacle_init_x = rng.uniform(self.env_x_lb, self.env_x_ub)
                    obstacle_init_y = rng.uniform(self.env_y_lb, self.env_y_ub)
                    obstacle_init_yaw = rng.uniform(-np.pi, np.pi)
                    obstacle_width = rng.uniform(self.stick_w_lb, self.stick_w_ub)
                    obstacle_height = rng.uniform(self.stick_h_lb, self.stick_h_ub)
                    obstacle_rect = utils.Rectangle(obstacle_init_x, obstacle_init_y,
                                                obstacle_width, obstacle_height,
                                                obstacle_init_yaw)
                    obstacle_collision = self.check_collision(tmp_state, obstacle_rect,
                                                              ignore_objects)
                obstacle = Object(f"stick{j}", self._stick_type)
                held = 0.0
                data[obstacle] = np.array([
                    obstacle_init_x, obstacle_init_y, obstacle_width, obstacle_height,
                    obstacle_init_yaw, held
                ],
                                      dtype=np.float32)

            state = State(data)
            tasks.append(Task(state, goal))
        return tasks

    # From https://stackoverflow.com/questions/37101001/pythonic-way-to-generate-random-uniformly-distributed-points-within-hollow-squarl
    def sample_outside_bbox(self,
                            a,
                            b,
                            inner_width,
                            inner_height,
                            rng,
                            n_samples=1):
        """(a, b) is the lower-left corner of the "hollow"."""
        outer_width = self.env_x_ub - self.env_x_lb
        outer_height = self.env_y_ub - self.env_y_lb
        llcorners = np.array([[0, 0], [a, 0], [a + inner_width, 0], [0, b],
                              [a + inner_width, b], [0, b + inner_height],
                              [a, b + inner_height],
                              [a + inner_width, b + inner_height]])
        top_height = outer_height - (b + inner_height)
        right_width = outer_width - (a + inner_width)
        widths = np.array([
            a, inner_width, right_width, a, right_width, a, inner_width,
            right_width
        ])
        heights = np.array([
            b, b, b, inner_height, inner_height, top_height, top_height,
            top_height
        ])
        areas = widths * heights
        shapes = np.column_stack((widths, heights))

        regions = rng.multinomial(n_samples, areas / areas.sum())
        indices = np.repeat(range(8), regions)
        unit_coords = rng.random(size=(n_samples, 2))
        pts = unit_coords * shapes[indices] + llcorners[indices]

        return pts + np.array([[self.env_x_lb, self.env_y_lb]])

    def check_collision(self,
                        state: State,
                        geom: utils._Geom2D,
                        ignore_objects: Optional[Set[Object]] = None) -> bool:
        if ignore_objects is None:
            ignore_objects = set()
        for obj in state.data:
            if obj in ignore_objects:
                continue
            if obj.is_instance(self._stick_type) or obj.is_instance(
                    self._basket_type):
                x = state.get(obj, "pose_x")
                y = state.get(obj, "pose_y")
                width = state.get(obj, "width")
                height = state.get(obj, "height")
                theta = state.get(obj, "yaw")
                while theta > np.pi:
                    theta -= (2 * np.pi)
                while theta < -np.pi:
                    theta += (2 * np.pi)
                obj_geom = utils.Rectangle(x, y, width, height, theta)
            elif obj.is_instance(self._robot_type):
                x = state.get(obj, "pose_x")
                y = state.get(obj, "pose_y")
                obj_geom = utils.Circle(x, y, self.robot_radius)
            else:
                assert obj.type.name == 'dummy'
                continue
            if utils.geom2ds_intersect(geom, obj_geom):
                return True
        return False

    '''
    Note: the pick policy takes a parameter delta, representing how far to stretch the
    gripper (between 0, 1 relative to the gripper size), and a parameter yaw, representing
    the relative orientation of the stick once gripped. 
    '''

    def _PickStick_policy(self, state: State, memory: Dict,
                         objects: Sequence[Object], params: Array) -> Action:
        del memory  # unused
        robby, stick = objects
        stick_x = state.get(stick, "pose_x")
        stick_y = state.get(stick, "pose_y")
        stick_w = state.get(stick, "width")
        stick_h = state.get(stick, "height")
        stick_yaw = state.get(stick, "yaw")

        robby_x = state.get(robby, "pose_x")
        robby_y = state.get(robby, "pose_y")
        robby_yaw = state.get(robby, "yaw")

        if CFG.bookshelf_add_sampler_idx_to_params:
            _, offset_gripper, stick_yaw = params
        else:
            offset_gripper, stick_yaw = params

        arr = np.array([robby_x, robby_y, stick_yaw, offset_gripper, -1],
                       dtype=np.float32)
        return Action(arr)

    '''
    Note: the place policy takes a single parameter delta, representing how far to stretch
    the gripper, between 0 and 1 relative to the gripper size. 
    '''

    def _PlaceStickOnBasket_policy(self, state: State, memory: Dict,
                                 objects: Sequence[Object],
                                 params: Array) -> Action:
        del memory  # unused
        robby, stick, basket = objects
        stick_x = state.get(stick, "pose_x")
        stick_y = state.get(stick, "pose_y")
        stick_w = state.get(stick, "width")
        stick_h = state.get(stick, "height")
        stick_yaw = state.get(stick, "yaw")

        basket_x = state.get(basket, "pose_x")
        basket_y = state.get(basket, "pose_y")
        basket_w = state.get(basket, "width")
        basket_h = state.get(basket, "height")
        basket_yaw = state.get(basket, "yaw")

        robby_x = state.get(robby, "pose_x")
        robby_y = state.get(robby, "pose_y")
        robby_yaw = state.get(robby, "yaw")

        if CFG.bookshelf_add_sampler_idx_to_params:
            _, offset_gripper = params
        else:
            offset_gripper, = params

        arr = np.array([robby_x, robby_y, robby_yaw, offset_gripper, 0],
                       dtype=np.float32)
        return Action(arr)

    '''
    Note: the navigation policy takes xy parameters between -.5 and 1.5 representing
    the relative placement wrt the object, normalized by the object's dimensions.
    '''

    def _NavigateTo_policy(self, state: State, memory: Dict,
                           objects: Sequence[Object], params: Array) -> Action:
        del memory  # unused
        robby, obj = objects
        obj_x = state.get(obj, "pose_x")
        obj_y = state.get(obj, "pose_y")
        obj_w = state.get(obj, "width")
        obj_h = state.get(obj, "height")
        obj_yaw = state.get(obj, "yaw")
        if CFG.use_fully_uniform_actions:
            if CFG.bookshelf_add_sampler_idx_to_params:
                _, param_pos_x, param_pos_y = params
            else:
                param_pos_x, param_pos_y = params

            # pos_x cos = obj_x cos + obj_w offset_x cos2 - obj_h offset_y sincos
            # pos_y sin = obj_y sin + obj_w offset_x sin2 + obj_h offset_y sincos
            # pos_x cos + pos_y sin = obj_x cos + obj_y sin + obj_w offset_x
            # offset_x = ((pos_x - obj_x) cos + (pos_y - obj_y) sin) / obj_w

            # pos_x sin = obj_x sin + obj_w offset_x sincos - obj_h offset_y sin2
            # pos_y cos = obj_y cos + obj_w offset_x sincos + obj_h offset_y cos2
            # pos_y cos - pos_x sin = obj_y cos - obj_x sin + obj_h offset_y
            # offset_y = ((pos_y - obj_y) cos - (pos_x - obj_x) sin) / obj_h

            offset_x = ((param_pos_x - obj_x) * np.cos(obj_yaw) + (param_pos_y - obj_y) * np.sin(obj_yaw)) / obj_w
            offset_y = ((param_pos_y - obj_y) * np.cos(obj_yaw) - (param_pos_x - obj_x) * np.sin(obj_yaw)) / obj_h
        else:
            if CFG.bookshelf_add_sampler_idx_to_params:
                _, offset_x, offset_y = params
            else:
                offset_x, offset_y = params

        pos_x = obj_x + obj_w * offset_x * np.cos(obj_yaw) - \
                obj_h * offset_y * np.sin(obj_yaw)
        pos_y = obj_y + obj_w * offset_x * np.sin(obj_yaw) + \
                obj_h * offset_y * np.cos(obj_yaw)

        if offset_x < 0 and 0 <= offset_y <= 1:
            yaw = 0
        elif offset_x > 1 and 0 <= offset_y <= 1:
            yaw = -np.pi
        elif 0 <= offset_x <= 1 and offset_y < 0:
            yaw = np.pi / 2
        elif 0 <= offset_x <= 1 and offset_y > 1:
            yaw = -np.pi / 2
        elif 0 <= offset_x <= 1 and 0 <= offset_y <= 1:
            # Collision with object; will fail, so set any value
            yaw = 0
        else:
            x = offset_x - 1 if offset_x > 1 else offset_x
            y = offset_y - 1 if offset_y > 1 else offset_y
            yaw = np.arctan2(-y * obj_h, -x * obj_w)
        yaw += obj_yaw
        while yaw > np.pi:
            yaw -= (2 * np.pi)
        while yaw < -np.pi:
            yaw += (2 * np.pi)
        arr = np.array([pos_x, pos_y, yaw, 0.0, 1.0], dtype=np.float32)
        # pos_x and pos_y might take the robot out of the env bounds, so we clip
        # the action back into its bounds for safety.
        arr = np.clip(arr, self.action_space.low, self.action_space.high)
        return Action(arr)

    def _Holding_holds(self, state: State, objects: Sequence[Object]) -> bool:
        stick, = objects
        return state.get(stick, "held") == 1.0

    def _OnBasket_holds(self, state: State, objects: Sequence[Object]) -> bool:
        stick, basket = objects

        stick_x = state.get(stick, "pose_x")
        stick_y = state.get(stick, "pose_y")
        stick_w = state.get(stick, "width")
        stick_h = state.get(stick, "height")
        stick_yaw = state.get(stick, "yaw")

        basket_x = state.get(basket, "pose_x")
        basket_y = state.get(basket, "pose_y")
        basket_w = state.get(basket, "width")
        basket_h = state.get(basket, "height")
        basket_yaw = state.get(basket, "yaw")

        while stick_yaw > np.pi:
            stick_yaw -= (2 * np.pi)
        while stick_yaw < -np.pi:
            stick_yaw += (2 * np.pi)
        stick_rect = utils.Rectangle(stick_x, stick_y, stick_w, stick_h, stick_yaw)
        basket_rect = utils.Rectangle(basket_x, basket_y, basket_w, basket_h,
                                     basket_yaw)
        return basket_rect.contains_point(*(stick_rect.center))

    def _GripperFree_holds(self, state: State,
                           objects: Sequence[Object]) -> bool:
        robby, = objects
        return state.get(robby, "gripper_free") == 1.0

    def _CanReach_holds(self, state: State,
                        objects: Sequence[Object]) -> bool:
        obj, robby = objects
        if obj.is_instance(self._stick_type) and self._Holding_holds(state, [obj]):
            return False
        if obj.is_instance(self._stick_type) or obj.is_instance(self._basket_type):
            x = state.get(obj, "pose_x")
            y = state.get(obj, "pose_y")
            width = state.get(obj, "width")
            height = state.get(obj, "height")
            theta = state.get(obj, "yaw")
            while theta > np.pi:
                theta -= (2 * np.pi)
            while theta < -np.pi:
                theta += (2 * np.pi)
            obj_geom = utils.Rectangle(x, y, width, height, theta)
        else:
            raise ValueError("Can only compute reachable for sticks and bins")

        robby_x = state.get(robby, "pose_x")
        robby_y = state.get(robby, "pose_y")
        robby_yaw = state.get(robby, "yaw")
        tip_x = robby_x + (self.robot_radius + self.gripper_length) * np.cos(robby_yaw)
        tip_y = robby_y + (self.robot_radius + self.gripper_length) * np.sin(robby_yaw)
        gripper_line = utils.LineSegment(robby_x, robby_y, tip_x, tip_y)
        return utils.geom2ds_intersect(gripper_line, obj_geom)


    def _get_pickable_stick(self, state: State, tip_x: float, tip_y: float):
        pick_stick = None
        stick_rect = None
        for obj in state.data:
            if obj.is_instance(self._stick_type):
                x = state.get(obj, "pose_x")
                y = state.get(obj, "pose_y")
                width = state.get(obj, "width")
                height = state.get(obj, "height")
                theta = state.get(obj, "yaw")
                while theta > np.pi:
                    theta -= (2 * np.pi)
                while theta < -np.pi:
                    theta += (2 * np.pi)
                stick_rect = utils.Rectangle(x, y, width, height, theta)
                if stick_rect.contains_point(tip_x, tip_y):
                    pick_stick = obj
                    break
        return pick_stick, stick_rect

    def _get_held_stick(self, state):
        for obj in state:
            if obj.is_instance(self._stick_type) and state.get(obj,
                                                              "held") == 1.0:
                return obj
        return None
